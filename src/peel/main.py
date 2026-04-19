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
from peel.db import DB
from peel.matcher import best_match
from peel.sources.rss import PitchforkBNT
from peel.spotify_client import SpotifyClient

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

    # Lista de URIs para adicionar à playlist
    new_uris: list[str] = []

    try:
        db.init_schema()

        # Sources a processar (hardcoded por agora, virá de config v2)
        sources = [PitchforkBNT()]

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

                # 2. Para cada track: match + record
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
                        new_uris.append(uri)
                        tracks_added += 1

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

        # 3. Adiciona todas as novas faixas à playlist
        if new_uris:
            try:
                sp.add_to_playlist(settings.peel_playlist_id, new_uris)
                log.info(
                    "playlist.updated",
                    playlist_id=settings.peel_playlist_id,
                    tracks_added=len(new_uris),
                )
            except Exception as e:
                log.exception(
                    "playlist.add_failed",
                    playlist_id=settings.peel_playlist_id,
                    error=str(e),
                )
                raise
        else:
            log.info("playlist.no_new_tracks")

    finally:
        # 4. Fecha DB (sempre, mesmo com erros)
        db.close()

        # 5. Log final com totais
        duration_seconds = (datetime.now(UTC) - start_time).total_seconds()
        log.info(
            "run.completed",
            sources_processed=sources_processed,
            tracks_added=tracks_added,
            tracks_unmatched=tracks_unmatched,
            duration_seconds=duration_seconds,
        )


if __name__ == "__main__":
    run()
