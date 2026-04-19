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

from datetime import UTC, datetime

import structlog

from peel.config import settings
from peel.db import DB, iso_week
from peel.matcher import best_match
from peel.sources.rss import GorillaVsBear, PitchforkBNT, StereogumNewMusic, TheQuietus
from peel.spotify_client import SpotifyClient
from peel.telegram import send_digest

# Setup de logging estruturado (JSON para GitHub Actions)
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)

log = structlog.get_logger()


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

    # Digest semanal: tracks e álbuns novos (para Telegram)
    new_track_entries: list[tuple[str, str, str | None]] = []  # (artist, title, url)
    new_album_entries: list[tuple[str, str, str | None]] = []  # (artist, album, url)

    try:
        db.init_schema()

        # Sources a processar (hardcoded por agora, virá de config v2)
        sources = [PitchforkBNT(), StereogumNewMusic(), TheQuietus(), GorillaVsBear()]

        for source in sources:
            sources_processed += 1

            try:
                # 1. Fetch da source
                tracks = source.fetch()
                log.info(
                    "source.fetched",
                    source_id=source.id,
                    track_count=len(tracks),
                )

                # 2. Bifurca por source.kind
                if source.kind == "album":
                    # Processa como álbuns (sem Spotify search)
                    for track in tracks:
                        try:
                            # track.title é o nome do álbum
                            is_new = db.record_album(
                                track.artist,
                                track.title,
                                source.id,
                                track.source_url,
                            )

                            if is_new:
                                albums_added += 1
                                new_album_entries.append(
                                    (track.artist, track.title, track.source_url)
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

                else:
                    # Processa como tracks (padrão "track")
                    for track in tracks:
                        try:
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
                                db.record_unmatched(source.id, track.artist, track.title)
                                tracks_unmatched += 1
                                log.warning(
                                    "track.no_match",
                                    source_id=source.id,
                                    artist=track.artist,
                                    title=track.title,
                                )
                                continue

                            # Verifica se já foi adicionada
                            if db.already_added(uri):
                                log.debug(
                                    "track.already_added",
                                    source_id=source.id,
                                    uri=uri,
                                )
                                continue

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
                            db.record_track(
                                uri,
                                source.id,
                                track.artist,
                                track.title,
                                track.source_url,
                            )
                            tracks_added += 1
                            new_track_entries.append((track.artist, track.title, track.source_url))

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

                # Atualiza estado da source como OK
                db.update_source_state(source.id, "ok")
                log.info("source.completed", source_id=source.id, status="ok")

            except Exception as e:
                # Source falhou — regista e continua com a próxima
                log.exception(
                    "source.failed",
                    source_id=source.id,
                    error=str(e),
                )
                db.update_source_state(source.id, "error", str(e))
                continue

        # 3. Rotação: substitui playlist pelos URIs da janela recente
        current_week = iso_week(datetime.now(UTC))
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
            send_digest(new_track_entries, new_album_entries, settings.peel_playlist_id)
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
            duration_seconds=duration_seconds,
        )


if __name__ == "__main__":
    run()
