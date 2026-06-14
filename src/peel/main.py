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

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from peel.config import settings
from peel.db import DB, iso_week
from peel.matcher import best_match, normalize
from peel.models import Track
from peel.scoring import SourceScore, build_source_scores
from peel.sources.rss import (
    GorillaVsBear,
    GuardianMusicAlbums,
    NprNewMusicFridayStarting5,
    PitchforkBNT,
    StereogumNewMusic,
    TheQuietus,
    TheQuietusTracksOfMonth,
)
from peel.spotify_client import SpotifyClient
from peel.telegram import DigestItem, send_digest

# Setup de logging estruturado (JSON para GitHub Actions)
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)

log = structlog.get_logger()


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


def run() -> None:
    """Executa uma run semanal do Peel.

    - Fetch de sources (Pitchfork, etc.)
    - Matching com Spotify
    - Adição à playlist
    - Logging de resultados
    """
    # Timestamp de início (para duration logging)
    start_time = datetime.now(UTC)

    # Inicializar DB e SpotifyClient
    db = DB(settings.db_path)
    sp = SpotifyClient()

    # Contadores
    sources_processed = 0
    tracks_added = 0
    tracks_unmatched = 0
    albums_added = 0

    # Digest semanal: tracks, álbuns e escutas externas novas (para Telegram)
    new_track_entries: list[DigestItem] = []  # (source_id, artist, title, url)
    new_album_entries: list[DigestItem] = []  # (source_id, artist, album, url)
    external_entries: list[DigestItem] = []  # unmatched com URL para ouvir fora do Spotify

    # Métricas do retry de unmatched (reportadas no log final)
    retried_total = 0
    retried_matched = 0

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

        # 0. Retry de tracks unmatched recentes — muitos blogs publicam picks
        # antes do release global de sexta, ou os tracks chegam ao Spotify com
        # dias/semanas de atraso. Reprocessa antes das sources novas para maximizar
        # chances de recuperação.
        digest_count_before_retry = len(new_track_entries)
        retried_total, retried_matched = _retry_unmatched(
            db,
            sp,
            new_track_entries,
            max_new_tracks=settings.peel_max_tracks_per_run,
            banned_track_keys=banned_track_keys,
        )
        tracks_added += len(new_track_entries) - digest_count_before_retry
        playlist_slots_used = tracks_added

        # Sources a processar (hardcoded por agora, virá de config v2)
        sources = [
            PitchforkBNT(),
            StereogumNewMusic(),
            TheQuietus(),
            TheQuietusTracksOfMonth(),
            GorillaVsBear(),
            GuardianMusicAlbums(),
            NprNewMusicFridayStarting5(),
        ]

        for source in sources:
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
                            )

                            if is_new:
                                albums_added += 1
                                source_stats.album_count += 1
                                new_album_entries.append(
                                    (source.id, track.artist, track.title, track.source_url)
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

                            if _track_cap_reached(playlist_slots_used):
                                source_stats.skipped_cap_count += 1
                                log.info(
                                    "track.skipped_global_cap",
                                    source_id=source.id,
                                    artist=track.artist,
                                    title=track.title,
                                    max_tracks_per_run=settings.peel_max_tracks_per_run,
                                )
                                continue
                            playlist_slots_used += 1

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
                                if track.source_url:
                                    external_entries.append(
                                        (source.id, track.artist, track.title, track.source_url)
                                    )
                                log.warning(
                                    "track.no_match",
                                    source_id=source.id,
                                    artist=track.artist,
                                    title=track.title,
                                )
                                continue

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

                            # TRADE-OFF de design: registamos a track no DB ANTES de a
                            # adicionar à playlist. Se add_to_playlist falhar depois,
                            # essa track fica "órfã" — marcada como added no DB mas nunca
                            # entregue ao Spotify. Aceitamos este trade-off porque:
                            # (1) Falhas do Spotify são raras e transientes
                            # (2) A próxima run do cron trará novas faixas (evolução normal)
                            # (3) Implementar two-phase commit (commit do DB apenas após
                            #     add_to_playlist bem-sucedido) duplicaria complexidade sem
                            #     ganho proporcional. Eventos de falha podem ser auditados
                            #     via logs estruturados.
                            inserted = db.record_track(
                                uri,
                                source.id,
                                track.artist,
                                track.title,
                                track.source_url,
                            )

                            if already:
                                log.debug(
                                    "track.attributed_existing",
                                    source_id=source.id,
                                    uri=uri,
                                    inserted=inserted,
                                )
                                continue

                            tracks_added += 1
                            source_stats.new_unique_count += 1
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

        # 3. Rotação: substitui playlist pelos URIs da janela recente
        current_week = iso_week(datetime.now(UTC))
        try:
            window_uris = db.ranked_tracks_in_window(
                current_week,
                settings.peel_playlist_window_weeks,
                source_quality,
            )
        except Exception as e:
            log.exception("playlist.ranking_failed", error=str(e))
            window_uris = db.tracks_in_window(current_week, settings.peel_playlist_window_weeks)
        try:
            sp.replace_playlist_items(settings.peel_playlist_id, window_uris)
            log.info(
                "playlist.rotated",
                playlist_id=settings.peel_playlist_id,
                track_count=len(window_uris),
                window_weeks=settings.peel_playlist_window_weeks,
                current_week=current_week,
            )
        except Exception as e:
            log.exception(
                "playlist.replace_failed",
                playlist_id=settings.peel_playlist_id,
                error=str(e),
            )
            # Não levantamos — digest ainda vai enviar

    finally:
        # 4. Envia digest semanal (SEMPRE — mesmo que playlist tenha falhado).
        #    send_digest tem a sua própria protecção contra HTTP errors.
        try:
            send_digest(
                _with_source_counts(db, new_track_entries),
                new_album_entries,
                settings.peel_playlist_id,
                external_entries=external_entries,
            )
        except Exception:
            log.exception("digest.crashed")

        # 5. Fecha DB (sempre, mesmo com erros)
        db.close()

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
