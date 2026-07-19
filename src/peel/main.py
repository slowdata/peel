"""Orquestração da run semanal do Peel.

Fluxo:
1. Inicializa DB + SpotifyClient
2. Para cada source: fetch tracks, match com Spotify, adiciona à playlist
3. Logs estruturados (JSON) para GitHub Actions
4. Fecha DB (liberta locks WAL)

Resiliência:
- Falha de uma source não para as outras (try/except por source)
- Falha no matching não para a run (try/except por faixa)
- db.close() é chamado mesmo com crashes (finally)
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from peel.affinity import AffinityProfile, build_affinity_profile
from peel.albums import AlbumRecommendation, spotify_album_url, top_album_recommendations
from peel.config import settings
from peel.db import DB, iso_week
from peel.matcher import best_match, normalize
from peel.models import Track
from peel.scoring import SourceScore, build_source_scores
from peel.site_export import make_album_resolver, spotify_album_search_url
from peel.sources.registry import active_sources
from peel.spotify_client import SpotifyClient
from peel.telegram import AlbumPickItem, DigestItem, TriageItem, send_digest

# Setup de logging estruturado (JSON para GitHub Actions)
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    """Metadados locais para aplicar diversidade suave aos pendentes.

    Consenso mantém prioridade lexicográfica. A qualidade da source é um único
    score comparável, já com feedback incluído, e sofre diminishing returns à
    medida que a mesma source entra na fila pendente.
    """

    source_id: str
    source_count: int
    source_score: float
    affinity: float
    latest_at: float


# Penalização linear por escolha pendente já atribuída à mesma source. A escala
# cobre uma fila de 28 sem criar quota/cap: diferenças de qualidade extremas
# continuam a poder justificar domínio, mas qualidade próxima não monopoliza.
SOURCE_REPEAT_PENALTY = 2.0


@dataclass(slots=True)
class SourceRunStats:
    source_id: str
    run_at: str
    fetched_count: int = 0
    fresh_count: int = 0
    processed_count: int = 0
    matched_count: int = 0
    new_unique_count: int = 0
    unmatched_count: int = 0
    album_count: int = 0
    skipped_stale_count: int = 0
    skipped_cap_count: int = 0

    def record(self, db: DB, status: str, error: str | None = None) -> None:
        db.record_source_run(
            source_id=self.source_id,
            run_at=self.run_at,
            fetched_count=self.fetched_count,
            fresh_count=self.fresh_count,
            processed_count=self.processed_count,
            matched_count=self.matched_count,
            new_unique_count=self.new_unique_count,
            unmatched_count=self.unmatched_count,
            album_count=self.album_count,
            skipped_stale_count=self.skipped_stale_count,
            skipped_cap_count=self.skipped_cap_count,
            status=status,
            error=error,
        )


def run(dry_run: bool = False) -> None:
    """Executa uma run semanal do Peel.

    - Fetch de sources (Pitchfork, etc.)
    - Matching com Spotify
    - Adição à playlist
    - Logging de resultados
    """
    # Timestamp de início (para duration logging)
    start_time = datetime.now(UTC)

    # Dry run: opera numa CÓPIA descartável da DB, não escreve playlists nem
    # envia Telegram. As escritas (record_track, etc.) vão para a cópia e são
    # deitadas fora no fim.
    db_path = settings.db_path
    if dry_run:
        fd, db_path = tempfile.mkstemp(prefix="peel-dryrun-", suffix=".db")
        os.close(fd)
        shutil.copyfile(settings.db_path, db_path)
        log.info("run.dry_run_started", db_copy=db_path)

    # Inicializar DB e SpotifyClient
    db = DB(db_path)
    sp = SpotifyClient()
    album_resolver = make_album_resolver(sp)

    # Contadores
    sources_processed = 0
    tracks_added = 0
    tracks_unmatched = 0
    albums_added = 0

    # Tracks novas da run servem para o cap/retry; Telegram usa a triagem efectiva.
    new_track_entries: list[DigestItem] = []  # (source_id, artist, title, url)
    new_track_uris: list[str] = []
    triage_entries: list[TriageItem] = []
    triage_ready = False
    playlist_updated = False
    new_album_entries: list[DigestItem] = []  # (source_id, artist, album, url)
    album_recommendations: list[AlbumRecommendation] = []

    # Métricas do retry de unmatched (reportadas no log final)
    retried_total = 0
    retried_matched = 0

    # Defaults para o finally: se a run rebentar antes do scoring, o digest
    # continua best-effort sem affinity/source quality.
    source_quality: dict[str, tuple[float, float]] = {}
    affinity_profile = build_affinity_profile()

    try:
        db.init_schema()

        # Feedback acionável: bans e caps por source são carregados uma vez por
        # run para evitar queries repetidas dentro dos loops. `ban` é semântica
        # de faixa/sugestão, não bloqueio automático de artista.
        banned_track_keys = _load_banned_track_keys(db)
        source_scores = _load_source_scores(db)
        source_slot_caps = _build_source_slot_caps(
            source_scores, settings.peel_max_tracks_per_source
        )
        source_quality = _source_quality_map(source_scores)
        affinity_profile = _load_affinity_profile(db)

        # 0. Retry de tracks unmatched recentes — muitos blogs publicam picks
        # antes do release global de sexta, ou os tracks chegam ao Spotify com
        # dias/semanas de atraso. Reprocessa antes das sources novas para maximizar
        # chances de recuperação.
        digest_count_before_retry = len(new_track_entries)
        retried_total, retried_matched = _retry_unmatched(
            db,
            sp,
            new_track_entries,
            new_track_uris=new_track_uris,
            max_new_tracks=settings.peel_max_tracks_per_run,
            banned_track_keys=banned_track_keys,
        )
        tracks_added += len(new_track_entries) - digest_count_before_retry
        playlist_slots_used = tracks_added

        # Sources a processar: registo declarativo em sources/registry.py.
        # Adicionar/remover/desligar uma source não deve exigir mexer neste
        # orquestrador.
        for source in active_sources():
            sources_processed += 1
            source_stats = SourceRunStats(
                source_id=source.id,
                run_at=datetime.now(UTC).isoformat(),
            )

            try:
                # 1. Fetch da source
                tracks = source.fetch()
                fresh_tracks = _filter_fresh_source_items(source.id, tracks, datetime.now(UTC))
                source_stats.fetched_count = len(tracks)
                source_stats.fresh_count = len(fresh_tracks)
                source_stats.skipped_stale_count = len(tracks) - len(fresh_tracks)
                log.info(
                    "source.fetched",
                    source_id=source.id,
                    track_count=len(tracks),
                    fresh_count=len(fresh_tracks),
                )

                # 2. Bifurca por source.kind
                if source.kind == "album":
                    # Processa como álbuns (sem Spotify search)
                    for track in fresh_tracks:
                        try:
                            source_stats.processed_count += 1
                            # track.title é o nome do álbum
                            is_new = db.record_album(
                                track.artist,
                                track.title,
                                source.id,
                                track.source_url,
                                spotify_album_uri=track.spotify_album_uri,
                            )

                            if is_new:
                                albums_added += 1
                                source_stats.album_count += 1
                                new_album_entries.append(
                                    (
                                        source.id,
                                        track.artist,
                                        track.title,
                                        _album_listen_url(
                                            track.artist,
                                            track.title,
                                            track.spotify_album_uri,
                                            [track.source_url],
                                            album_resolver,
                                        ),
                                    )
                                )
                                log.info(
                                    "album.recorded",
                                    source_id=source.id,
                                    artist=track.artist,
                                    album=track.title,
                                )

                        except Exception as e:
                            log.exception(
                                "album.processing_failed",
                                source_id=source.id,
                                artist=track.artist,
                                album=track.title,
                                error=str(e),
                            )
                            continue

                elif source.kind == "track":
                    # Processa como tracks (único kind que pode ir para playlist).
                    # O slice por source evita backfill infinito de feeds longos: a próxima
                    # run volta a olhar para os mesmos N itens do topo, não para backlog.
                    source_cap = source_slot_caps.get(
                        source.id,
                        settings.peel_max_tracks_per_source,
                    )
                    source_candidates = fresh_tracks[:source_cap]
                    source_stats.skipped_cap_count += max(
                        0, len(fresh_tracks) - len(source_candidates)
                    )
                    for track in source_candidates:
                        try:
                            if _track_key(track.artist, track.title) in banned_track_keys:
                                log.info(
                                    "track.skipped_banned",
                                    source_id=source.id,
                                    artist=track.artist,
                                    title=track.title,
                                    reason="artist_title",
                                )
                                continue

                            # Searching e atribuição de consenso procedem sempre (são
                            # baratas e enriquecem a qualificação da faixa). O cap global
                            # limita só as tracks NOVAS que sobem ao digest/playlist — assim
                            # faixas unmatched não "queimam" slots e fontes tardias (ex.: NPR)
                            # não são starvationadas por unmatched de fontes cedo.
                            source_stats.processed_count += 1

                            # Busca candidatos no Spotify
                            candidates = sp.search_track(track.artist, track.title, limit=5)

                            # Encontra melhor match
                            uri = best_match(
                                track,
                                candidates,
                                threshold=settings.match_threshold,
                            )

                            if uri is None:
                                # Não encontrou match
                                db.record_unmatched(
                                    source.id,
                                    track.artist,
                                    track.title,
                                    track.source_url,
                                )
                                tracks_unmatched += 1
                                source_stats.unmatched_count += 1
                                log.warning(
                                    "track.no_match",
                                    source_id=source.id,
                                    artist=track.artist,
                                    title=track.title,
                                )
                                continue

                            canonical_uri = db.canonical_uri_for_track_identity(
                                track.artist,
                                track.title,
                            )
                            if canonical_uri is not None and canonical_uri != uri:
                                log.info(
                                    "track.canonical_uri_reused",
                                    source_id=source.id,
                                    matched_uri=uri,
                                    canonical_uri=canonical_uri,
                                )
                                uri = canonical_uri

                            if db.is_banned_uri(uri):
                                log.info(
                                    "track.skipped_banned",
                                    source_id=source.id,
                                    artist=track.artist,
                                    title=track.title,
                                    uri=uri,
                                    reason="uri",
                                )
                                continue

                            source_stats.matched_count += 1
                            already = db.already_added(uri)

                            if already:
                                # Consenso: atribui uma fonte nova a um URI já conhecido.
                                # Sempre registado — não conta para o cap de novidades.
                                inserted = db.record_track(
                                    uri,
                                    source.id,
                                    track.artist,
                                    track.title,
                                    track.source_url,
                                )
                                log.debug(
                                    "track.attributed_existing",
                                    source_id=source.id,
                                    uri=uri,
                                    inserted=inserted,
                                )
                                continue

                            # Brand-new URI — sujeito ao cap de NOVIDADES da semana.
                            # Verificamos o cap ANTES de registar: o digest e a playlist
                            # de triagem ficam alinhados (só tracks registadas entram na
                            # janela de rotação), faixas capped não poluem o histórico,
                            # e unmatched não queimam slots (o search já aconteceu acima).
                            # Serão redescobertas numa run futura se ainda forem frescas.
                            if _track_cap_reached(playlist_slots_used):
                                source_stats.skipped_cap_count += 1
                                log.info(
                                    "track.skipped_global_cap",
                                    source_id=source.id,
                                    artist=track.artist,
                                    title=track.title,
                                    uri=uri,
                                    max_tracks_per_run=settings.peel_max_tracks_per_run,
                                )
                                continue
                            playlist_slots_used += 1

                            # TRADE-OFF de design: registamos a track no DB ANTES de a
                            # adicionar à playlist. Se replace_playlist_items falhar
                            # depois, essa track fica "órfã" — marcada como added no DB
                            # mas nunca entregue ao Spotify. Aceitamos este trade-off
                            # porque: (1) Falhas do Spotify são raras e transientes;
                            # (2) A próxima run trará novas faixas (evolução normal);
                            # (3) Two-phase commit duplicaria complexidade sem ganho
                            #     proporcional. Eventos de falha são auditáveis via logs.
                            inserted = db.record_track(
                                uri,
                                source.id,
                                track.artist,
                                track.title,
                                track.source_url,
                            )

                            tracks_added += 1
                            source_stats.new_unique_count += 1
                            new_track_uris.append(uri)
                            new_track_entries.append(
                                (source.id, track.artist, track.title, track.source_url)
                            )

                            log.info(
                                "track.matched_and_added",
                                source_id=source.id,
                                artist=track.artist,
                                title=track.title,
                                uri=uri,
                            )

                        except Exception as e:
                            log.exception(
                                "track.processing_failed",
                                source_id=source.id,
                                artist=track.artist,
                                title=track.title,
                                error=str(e),
                            )
                            continue

                else:
                    log.info(
                        "source.skipped_non_playlist_kind",
                        source_id=source.id,
                        kind=source.kind,
                        fetched_count=len(fresh_tracks),
                    )

                # Atualiza estado da source como OK
                db.update_source_state(source.id, "ok")
                source_stats.record(db, "ok")
                log.info("source.completed", source_id=source.id, status="ok")

            except Exception as e:
                # Source falhou — regista e continua com a próxima
                log.exception(
                    "source.failed",
                    source_id=source.id,
                    error=str(e),
                )
                db.update_source_state(source.id, "error", str(e))
                source_stats.record(db, "error", str(e))
                continue

        # 2.5 Prune rows unmatched expiradas (desistimos após a janela de retry)
        pruned = db.prune_unmatched(settings.unmatched_retry_days)
        if pruned:
            log.info("unmatched.pruned", count=pruned, max_age_days=settings.unmatched_retry_days)

        # 3. Rotação. A triagem é a fila efectiva para ouvir: todas as tracks
        #    novas desta run entram primeiro; pendentes sem feedback só ocupam
        #    vagas livres. A playlist final ("Weekly") é construída depois por
        #    `peel finalize` a partir dos keepers.
        current_week = iso_week(datetime.now(UTC))
        review_id = settings.peel_review_playlist_id
        target_playlist = review_id or settings.peel_playlist_id
        # A triagem usa uma janela mais larga (várias semanas) para acumular
        # material de avaliação; a playlist final usa a janela curta.
        rotation_weeks = (
            settings.peel_review_playlist_window_weeks
            if review_id
            else settings.peel_playlist_window_weeks
        )
        try:
            window_uris = db.ranked_tracks_in_window(
                current_week,
                rotation_weeks,
                source_quality,
                affinity_profile.score,
            )
            current_week_uris = db.ranked_tracks_in_window(
                current_week,
                1,
                source_quality,
                affinity_profile.score,
            )
            candidate_metadata = _load_review_candidate_metadata(
                db,
                window_uris,
                source_quality,
                affinity_profile,
            )
            window_uris = select_review_playlist_uris(
                window_uris,
                current_week_uris,
                new_track_uris,
                lambda uri: not db.has_feedback_for_track_identity(uri),
                limit=settings.peel_max_tracks_per_run,
                candidate_metadata=candidate_metadata,
            )
            triage_entries = build_triage_items(
                db,
                window_uris,
                set(new_track_uris),
                current_week,
                source_quality,
                affinity_profile,
            )
            triage_ready = len(triage_entries) == len(window_uris)
            if triage_ready:
                distribution = dict(
                    sorted(Counter(item.source_id for item in triage_entries).items())
                )
                new_distribution = dict(
                    sorted(
                        Counter(item.source_id for item in triage_entries if item.is_new).items()
                    )
                )
                pending_distribution = dict(
                    sorted(
                        Counter(
                            item.source_id for item in triage_entries if not item.is_new
                        ).items()
                    )
                )
                log.info(
                    "playlist.triage_source_distribution",
                    sources=distribution,
                    new_sources=new_distribution,
                    pending_sources=pending_distribution,
                )
            if not triage_ready:
                log.error(
                    "playlist.triage_incomplete",
                    expected_tracks=len(window_uris),
                    digest_tracks=len(triage_entries),
                )
        except Exception as e:
            log.exception("playlist.triage_selection_failed", error=str(e))
            window_uris = []
        try:
            album_recommendations = top_album_recommendations(
                db,
                current_week,
                weeks=2,
                limit=7,
                source_quality=source_quality,
            )
        except Exception as e:
            log.exception("albums.recommendations_failed", error=str(e))
            album_recommendations = []

        if not triage_ready:
            log.error("playlist.rotation_skipped", reason="triage_selection_failed")
        elif dry_run:
            log.info(
                "playlist.rotation_skipped_dry_run",
                playlist_id=target_playlist,
                track_count=len(window_uris),
                current_week=current_week,
            )
        else:
            try:
                sp.replace_playlist_items(target_playlist, window_uris)
                db.replace_review_queue(target_playlist, triage_entries)
                playlist_updated = True
                log.info(
                    "playlist.rotated",
                    playlist_id=target_playlist,
                    track_count=len(window_uris),
                    is_review=bool(review_id),
                    current_week=current_week,
                    dry_run=False,
                )
            except Exception as e:
                log.exception(
                    "playlist.replace_failed",
                    playlist_id=target_playlist,
                    error=str(e),
                )

    finally:
        # 4. Telegram só pode anunciar a fila que Spotify confirmou. Dry-run é
        #    completamente sem efeitos externos; unmatched fica no relatório/DB.
        if dry_run:
            log.info("digest.skipped", reason="dry_run")
        elif not playlist_updated:
            log.warning("digest.skipped", reason="playlist_not_updated")
        else:
            try:
                send_digest(
                    triage_entries,
                    new_album_entries,
                    settings.peel_review_playlist_id or settings.peel_playlist_id,
                    album_recommendations=_album_digest_items(
                        album_recommendations, album_resolver
                    ),
                )
            except Exception:
                log.exception("digest.crashed")

        # 5. Fecha DB (sempre, mesmo com erros)
        db.close()

        # 5b. Dry run: descarta a cópia temporária da DB (zero impacto no real).
        if dry_run and db_path != settings.db_path:
            with contextlib.suppress(OSError):
                os.remove(db_path)

        # 6. Log final com totais
        duration_seconds = (datetime.now(UTC) - start_time).total_seconds()
        log.info(
            "run.completed",
            sources_processed=sources_processed,
            tracks_added=tracks_added,
            tracks_unmatched=tracks_unmatched,
            albums_added=albums_added,
            retried_total=retried_total,
            retried_matched=retried_matched,
            duration_seconds=duration_seconds,
        )


def _filter_fresh_source_items(source_id: str, tracks: list[Track], now: datetime) -> list[Track]:
    fresh_tracks: list[Track] = []
    cutoff = now - timedelta(days=settings.peel_max_source_item_age_days)
    for track in tracks:
        if track.published_at is None:
            fresh_tracks.append(track)
            continue

        published_at = track.published_at
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)

        if published_at < cutoff:
            log.info(
                "source.item_skipped_age",
                source_id=source_id,
                artist=track.artist,
                title=track.title,
                published_at=published_at.isoformat(),
                max_age_days=settings.peel_max_source_item_age_days,
            )
            continue
        fresh_tracks.append(track)
    return fresh_tracks


def _track_cap_reached(playlist_slots_used: int) -> bool:
    return playlist_slots_used >= settings.peel_max_tracks_per_run


def _track_key(artist: str, title: str) -> tuple[str, str]:
    """Identidade normalizada de uma faixa para evitar reentrada de bans."""
    return normalize(artist), normalize(title)


def _load_banned_track_keys(db: DB) -> set[tuple[str, str]]:
    """Carrega bans explícitos uma vez por run.

    Fail-open: se esta leitura falhar, a run continua. Em condições normais a
    DB já foi inicializada; falhas aqui indicam problema estrutural maior.
    """
    try:
        return db.banned_track_keys()
    except Exception as e:
        log.exception("feedback.bans_load_failed", error=str(e))
        return set()


def select_review_playlist_uris(
    ranked_uris: list[str],
    current_week_uris: list[str],
    new_track_uris: list[str],
    is_unrated: Callable[[str], bool],
    *,
    limit: int,
    candidate_metadata: Mapping[str, ReviewCandidate] | None = None,
) -> list[str]:
    """Constrói a fila de triagem: novas primeiro, pendentes só como fill.

    Todas as tracks novas desta run entram antes de qualquer faixa anterior.
    Como a própria ingestão aplica ``PEEL_MAX_TRACKS_PER_RUN``, o limite da
    triagem nunca deve cortar novidades. As vagas restantes são preenchidas com
    tracks sem feedback das semanas anteriores. Nessa fase apenas, repetições
    de source sofrem diminishing returns no score secundário; não há quotas,
    caps ou filtros de source.
    """
    if limit <= 0:
        return []

    new_uri_set = set(new_track_uris)
    new_uris = list(dict.fromkeys(uri for uri in current_week_uris if uri in new_uri_set))
    if len(new_uris) > limit:
        log.warning("playlist.new_tracks_exceed_cap", new_tracks=len(new_uris), limit=limit)
        return new_uris[:limit]

    selected_set = set(new_uris)
    pending_uris = [uri for uri in ranked_uris if uri not in selected_set and is_unrated(uri)]
    selected_pending = _select_diverse_pending_uris(
        pending_uris,
        candidate_metadata,
        limit=limit - len(new_uris),
    )
    result = new_uris + selected_pending
    log.info(
        "playlist.triage_selected",
        new_tracks=len(new_uris),
        pending_tracks=len(selected_pending),
        total=len(result),
        uris=result,
    )
    return result


def _select_diverse_pending_uris(
    pending_uris: list[str],
    candidate_metadata: Mapping[str, ReviewCandidate] | None,
    *,
    limit: int,
) -> list[str]:
    """Escolhe pendentes com diminishing returns por source, sem quotas.

    Consenso mantém precedência total. A qualidade da source entra uma única
    vez, já incluindo feedback real, e perde ``2.0`` por escolha anterior da
    mesma source. Alternativas de qualidade semelhante surgem mais cedo, mas
    uma source materialmente melhor pode continuar a dominar.
    """
    if limit <= 0:
        return []
    if not candidate_metadata:
        return pending_uris[:limit]
    missing_uris = [uri for uri in pending_uris if uri not in candidate_metadata]
    if missing_uris:
        log.warning(
            "playlist.triage_metadata_incomplete_fallback",
            missing_tracks=len(missing_uris),
            pending_tracks=len(pending_uris),
        )
        return pending_uris[:limit]

    remaining = list(enumerate(pending_uris))
    selected: list[str] = []
    source_counts: Counter[str] = Counter()

    while remaining and len(selected) < limit:

        def selection_key(
            item: tuple[int, str],
        ) -> tuple[float, float, float, float, int, str]:
            original_index, uri = item
            candidate = candidate_metadata[uri]
            repeat_penalty = SOURCE_REPEAT_PENALTY * source_counts[candidate.source_id]
            adjusted_source_score = candidate.source_score - repeat_penalty
            return (
                -float(candidate.source_count),
                -adjusted_source_score,
                -candidate.affinity,
                -candidate.latest_at,
                original_index,
                uri,
            )

        selected_index = min(
            range(len(remaining)), key=lambda index: selection_key(remaining[index])
        )
        _, uri = remaining.pop(selected_index)
        selected.append(uri)
        source_counts[candidate_metadata[uri].source_id] += 1

    return selected


def _load_review_candidate_metadata(
    db: DB,
    uris: list[str],
    source_quality: Mapping[str, tuple[float, float]],
    affinity_profile: AffinityProfile,
) -> dict[str, ReviewCandidate] | None:
    """Carrega metadados de diversidade, mantendo a rotação fail-open."""
    try:
        return _review_candidate_metadata(db, uris, source_quality, affinity_profile)
    except Exception as exc:  # noqa: BLE001 - a triagem pode usar ranking base
        log.warning("playlist.triage_metadata_failed_fallback", error=str(exc))
        return None


def _review_candidate_metadata(
    db: DB,
    uris: list[str],
    source_quality: Mapping[str, tuple[float, float]],
    affinity_profile: AffinityProfile,
) -> dict[str, ReviewCandidate]:
    """Reconstitui os sinais locais do ranking para pendentes da triagem.

    A source representativa usa a mesma regra de ``build_triage_items``. Só lê
    SQLite; não adiciona chamadas de rede nem altera a descoberta de tracks.
    """
    metadata: dict[str, ReviewCandidate] = {}
    for uri in uris:
        rows = db.conn.execute(
            """
            SELECT artist, title, source_id, added_at
            FROM tracks
            WHERE spotify_uri = ?
            """,
            (uri,),
        ).fetchall()
        if not rows:
            continue
        artist, title, source_id, _ = min(
            rows,
            key=lambda row: (
                -source_quality.get(str(row[2]), (0.0, 0.0))[0],
                -source_quality.get(str(row[2]), (0.0, 0.0))[1],
                str(row[2]),
            ),
        )
        latest_at = max(_timestamp_value(str(row[3])) for row in rows)
        _, source_score = source_quality.get(str(source_id), (0.0, 0.0))
        metadata[uri] = ReviewCandidate(
            source_id=str(source_id),
            source_count=_safe_source_count(db, str(artist), str(title)),
            source_score=source_score,
            affinity=affinity_profile.score(str(artist)),
            latest_at=latest_at,
        )
    return metadata


def _timestamp_value(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def slots_for_source(
    score: SourceScore | None,
    default: int,
    min_ratings: int = 5,
) -> int:
    """Calcula cap por source a partir do feedback recente.

    DECISÃO: só ajustamos depois de uma amostra mínima. Sources cold-start usam
    o default; sources boas ganham espaço; sources com rating médio negativo
    perdem espaço, mas mantêm pelo menos 2 slots para não matar descoberta.
    """
    if score is None or score.avg_rating is None or score.rating_count < min_ratings:
        return default
    if score.avg_rating >= 1.0:
        return default + 4
    if score.avg_rating < 0:
        return max(2, default - 4)
    return default


def _load_source_scores(db: DB) -> list[SourceScore]:
    """Carrega scoring uma vez; falha de scoring não pode rebentar a run."""
    try:
        return build_source_scores(db, weeks=4)
    except Exception as e:
        log.exception("source_scores.failed", error=str(e))
        return []


def _build_source_slot_caps(scores: list[SourceScore], default: int) -> dict[str, int]:
    """Constrói caps por source com base no scoring observacional recente."""
    caps = {score.source_id: slots_for_source(score, default) for score in scores}
    log.info("source_slots.computed", caps=caps, default=default)
    return caps


def _source_quality_map(scores: list[SourceScore]) -> dict[str, tuple[float, float]]:
    """Mapa usado para ranking da playlist: source -> (avg_rating, score)."""
    return {score.source_id: (score.avg_rating or 0.0, score.score) for score in scores}


def _load_affinity_profile(db: DB) -> AffinityProfile:
    """Carrega affinity local; falha não deve afectar descoberta."""
    try:
        return build_affinity_profile(db)
    except Exception as e:
        log.exception("affinity_profile.failed", error=str(e))
        return build_affinity_profile()


def build_triage_items(
    db: DB,
    uris: list[str],
    new_track_uris: set[str],
    current_week: str,
    source_quality: dict[str, tuple[float, float]],
    affinity_profile: AffinityProfile,
) -> list[TriageItem]:
    """Converte a playlist efectiva em linhas Telegram com estado explícito."""
    items: list[TriageItem] = []
    for uri in uris:
        rows = db.conn.execute(
            """
            SELECT artist, title, source_id, source_url, added_at_week
            FROM tracks
            WHERE spotify_uri = ?
            """,
            (uri,),
        ).fetchall()
        if not rows:
            continue
        best_artist, best_title, best_source, best_source_url, _ = min(
            rows,
            key=lambda row: (
                -source_quality.get(str(row[2]), (0.0, 0.0))[0],
                -source_quality.get(str(row[2]), (0.0, 0.0))[1],
                str(row[2]),
            ),
        )
        source_url = best_source_url or next((row[3] for row in rows if row[3]), None)
        added_at_week = min(str(row[4]) for row in rows)
        items.append(
            TriageItem(
                source_id=str(best_source),
                artist=str(best_artist),
                title=str(best_title),
                spotify_uri=uri,
                source_url=source_url,
                source_count=_safe_source_count(db, str(best_artist), str(best_title)),
                affinity=affinity_profile.score(str(best_artist)),
                is_new=uri in new_track_uris,
                added_at_week=added_at_week,
                current_week=current_week,
            )
        )
    return items


def _sort_track_digest_entries(
    db: DB,
    entries: list[DigestItem],
    source_quality: dict[str, tuple[float, float]],
    affinity_profile: AffinityProfile,
) -> list[DigestItem]:
    """Ordena digest por qualidade primária e affinity como desempate.

    Não filtra nem corta: só altera a ordem de apresentação.
    """
    indexed = list(enumerate(entries))

    def sort_key(item: tuple[int, DigestItem]) -> tuple[int, float, float, float, int]:
        index, entry = item
        source_id, artist, title, _ = entry[:4]
        source_count = _safe_source_count(db, artist, title)
        avg_rating, score = source_quality.get(source_id, (0.0, 0.0))
        affinity = affinity_profile.score(artist)
        return (-source_count, -avg_rating, -score, -affinity, index)

    return [entry for _, entry in sorted(indexed, key=sort_key)]


def _with_digest_metadata(
    db: DB,
    entries: list[DigestItem],
    affinity_profile: AffinityProfile,
) -> list[DigestItem]:
    """Enriquece digest com contagem de fontes e affinity para badges."""
    enriched: list[DigestItem] = []
    for source_id, artist, title, url in (entry[:4] for entry in entries):
        enriched.append(
            (
                source_id,
                artist,
                title,
                url,
                _safe_source_count(db, artist, title),
                affinity_profile.score(artist),
            )
        )
    return enriched


def _safe_source_count(db: DB, artist: str, title: str) -> int:
    try:
        return db.source_count_for_track_identity(artist, title)
    except Exception as e:
        log.exception(
            "digest.source_count_failed",
            artist=artist,
            title=title,
            error=str(e),
        )
        return 1


def _album_digest_items(
    recommendations: list[AlbumRecommendation],
    album_resolver: Callable[[str, str], str | None] | None = None,
) -> list[AlbumPickItem]:
    """Converte recomendações de álbuns para o formato compacto do Telegram.

    O link primário deve servir para ouvir: Spotify directo/resolvido, Bandcamp
    se for a fonte disponível, ou pesquisa Spotify como último recurso. O link
    editorial/source segue separado para o Telegram poder mostrar "Review" ou
    "Bandcamp" sem esconder a razão curatorial da escolha.
    """
    return [
        (
            item.artist,
            item.album,
            item.source_count,
            item.sources,
            _album_listen_url(
                item.artist,
                item.album,
                item.spotify_album_uri,
                (source_url for _, source_url in item.source_urls),
                album_resolver,
            ),
            _first_album_source_url(source_url for _, source_url in item.source_urls),
        )
        for item in recommendations
    ]


def _album_listen_url(
    artist: str,
    album: str,
    spotify_album_uri: str | None,
    source_urls: Iterable[str | None],
    album_resolver: Callable[[str, str], str | None] | None = None,
) -> str | None:
    """URL primário para ouvir um álbum no Telegram."""
    urls = tuple(source_urls)
    if spotify_album_uri:
        return spotify_album_url(spotify_album_uri)
    if album_resolver is not None:
        try:
            resolved = album_resolver(artist, album)
        except Exception as exc:  # noqa: BLE001 - digest não deve falhar por resolver externo
            log.warning("digest.album_resolver_failed", artist=artist, album=album, error=str(exc))
        else:
            if resolved:
                return resolved
    bandcamp_url = _first_bandcamp_url(urls)
    if bandcamp_url:
        return bandcamp_url
    return spotify_album_search_url(artist, album) or _first_album_source_url(urls)


def _first_album_source_url(source_urls: Iterable[str | None]) -> str | None:
    for source_url in source_urls:
        if source_url:
            return source_url
    return None


def _first_bandcamp_url(source_urls: Iterable[str | None]) -> str | None:
    for source_url in source_urls:
        if source_url and "bandcamp.com" in source_url:
            return source_url
    return None


def _with_source_counts(db: DB, entries: list[DigestItem]) -> list[DigestItem]:
    """Enriquece digest com nº de fontes para destacar consenso.

    Fail-open: se a consulta falhar para um item, mantém o formato antigo com
    4 campos e o Telegram mostra a track sem marca de consenso.
    """
    enriched: list[DigestItem] = []
    for source_id, artist, title, url in entries:
        try:
            source_count = db.source_count_for_track_identity(artist, title)
            enriched.append((source_id, artist, title, url, source_count))
        except Exception as e:
            log.exception(
                "digest.source_count_failed",
                source_id=source_id,
                artist=artist,
                title=title,
                error=str(e),
            )
            enriched.append((source_id, artist, title, url))
    return enriched


def _retry_unmatched(
    db: DB,
    sp: SpotifyClient,
    new_track_entries: list[DigestItem],
    new_track_uris: list[str] | None = None,
    max_new_tracks: int | None = None,
    banned_track_keys: set[tuple[str, str]] | None = None,
) -> tuple[int, int]:
    """Re-tenta tracks unmatched recentes contra o Spotify.

    Para cada (source_id, artist, title) unmatched dentro da janela configurada:
    - Procura candidatos no Spotify (com query normalizada — fix #1)
    - Se houver match acima do threshold, regista em tracks e apaga do unmatched
    - Se continuar sem match, fica — será tentada na próxima run até expirar.

    Returns:
        (total_retried, matched)
    """
    rows = db.list_unmatched_with_urls(settings.unmatched_retry_days)
    if not rows:
        return 0, 0

    total = len(rows)
    matched = 0
    banned_track_keys = banned_track_keys or _load_banned_track_keys(db)

    log.info("unmatched.retry_start", pending=total)

    for source_id, artist, title, source_url in rows:
        try:
            if _track_key(artist, title) in banned_track_keys:
                db.delete_unmatched(source_id, artist, title)
                log.info(
                    "track.skipped_banned",
                    source_id=source_id,
                    artist=artist,
                    title=title,
                    reason="artist_title",
                    phase="retry",
                )
                continue

            candidates = sp.search_track(artist, title, limit=5)
            if not candidates:
                continue

            # Usa Track temporário só para alimentar o matcher
            track = Track(source_id=source_id, artist=artist, title=title)
            uri = best_match(track, candidates, threshold=settings.match_threshold)
            if uri is None:
                continue

            canonical_uri = db.canonical_uri_for_track_identity(artist, title)
            if canonical_uri is not None:
                uri = canonical_uri

            if db.is_banned_uri(uri):
                db.delete_unmatched(source_id, artist, title)
                log.info(
                    "track.skipped_banned",
                    source_id=source_id,
                    artist=artist,
                    title=title,
                    uri=uri,
                    reason="uri",
                    phase="retry",
                )
                continue

            already = db.already_added(uri)
            if (
                not already
                and max_new_tracks is not None
                and len(new_track_entries) >= max_new_tracks
            ):
                log.info(
                    "unmatched.retry_skipped_cap",
                    source_id=source_id,
                    artist=artist,
                    title=title,
                    max_new_tracks=max_new_tracks,
                )
                continue

            inserted = db.record_track(uri, source_id, artist, title, source_url)
            db.delete_unmatched(source_id, artist, title)

            if already:
                log.debug(
                    "unmatched.retry_attributed_existing",
                    source_id=source_id,
                    artist=artist,
                    title=title,
                    uri=uri,
                    inserted=inserted,
                )
                matched += 1
                continue

            new_track_entries.append((source_id, artist, title, source_url))
            if new_track_uris is not None:
                new_track_uris.append(uri)
            matched += 1
            log.info(
                "unmatched.retry_matched",
                source_id=source_id,
                artist=artist,
                title=title,
                uri=uri,
            )
        except Exception as e:
            log.exception(
                "unmatched.retry_failed",
                source_id=source_id,
                artist=artist,
                title=title,
                error=str(e),
            )
            continue

    log.info("unmatched.retry_done", total=total, matched=matched)
    return total, matched


if __name__ == "__main__":
    run()
