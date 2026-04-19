"""Cliente Spotify minimal: search + playlist write.

Decisões de design:
1. Spotipy em vez de httpx directo: reduz boilerplate de auth.
2. Auth via refresh token: o access token expira em ~1h, mas cada run do Peel
   dura segundos — é aceitável refrescar a cada run.
3. search_track devolve list[dict] com múltiplos candidatos (não filtra nem
   normaliza aqui) — o matcher.py do Passo 5 faz a normalização e fuzzy match.
4. add_to_playlist em chunks de 100: limite hard da API. Sem chunks, fails se >100 URIs.
"""

from __future__ import annotations

import spotipy
import structlog
from spotipy.cache_handler import MemoryCacheHandler
from spotipy.oauth2 import SpotifyOAuth

from peel.config import settings

log = structlog.get_logger()

# Scopes necessários para add_to_playlist (write).
SCOPES = "playlist-modify-private playlist-modify-public"

# Redirect URI: tem de ser http://127.0.0.1:8888/callback (HTTP, 127.0.0.1, não localhost).
REDIRECT_URI = "http://127.0.0.1:8888/callback"


class SpotifyClient:
    """Cliente Spotify com auth via refresh token (ideal para CI/cron)."""

    def __init__(self) -> None:
        """Inicializa o cliente com OAuth usando refresh token.

        Em produção (GitHub Actions), o refresh_token vem de Secrets → variáveis de env.
        Em dev, vem do .env.

        Fluxo:
        1. SpotifyOAuth constrói o auth manager com refresh_token guardado.
        2. refresh_access_token() troca refresh_token por novo access_token.
        3. O access token expira em ~1h, mas cada run do Peel dura segundos.
           Se precisar de refresh automático dentro da run, spotipy faz via auth_manager.
        """
        auth_manager = SpotifyOAuth(
            client_id=settings.spotify_client_id,
            client_secret=settings.spotify_client_secret,
            redirect_uri=REDIRECT_URI,
            scope=SCOPES,
            cache_handler=MemoryCacheHandler(),
        )

        # Usa o refresh_token guardado em settings para obter um novo access_token
        token_info = auth_manager.refresh_access_token(settings.spotify_refresh_token)
        access_token = token_info["access_token"]

        # Cria o cliente Spotify com o access_token
        self.sp = spotipy.Spotify(auth=access_token)
        log.info("spotify_client.initialized")

    def search_track(self, artist: str, title: str, limit: int = 5) -> list[dict]:
        """Procura "artist title" no Spotify e devolve uma lista de candidatos.

        Args:
            artist: Nome do artista (não será normalizado aqui)
            title: Título da faixa (não será normalizado aqui)
            limit: Número máximo de resultados (default 5)

        Returns:
            Lista de dicts com {"uri": ..., "name": ..., "artists": [str, ...]}.
            Lista vazia [] se não encontrar ou em erro.

        Normalização e matching fuzzy ficam para o matcher.py (Passo 5).
        """
        query = f"{artist} {title}"

        try:
            results = self.sp.search(q=query, type="track", limit=limit)
            items = results.get("tracks", {}).get("items", [])

            if not items:
                log.warning(
                    "spotify.no_match",
                    query=query,
                    artist=artist,
                    title=title,
                )
                return []

            candidates = [
                {
                    "uri": item.get("uri"),
                    "name": item.get("name"),
                    "artists": [a["name"] for a in item.get("artists", [])],
                }
                for item in items
            ]

            log.debug(
                "spotify.search_results",
                query=query,
                count=len(candidates),
            )

            return candidates

        except Exception as e:
            log.exception(
                "spotify.search_failed",
                artist=artist,
                title=title,
                error=str(e),
            )
            return []

    def add_to_playlist(self, playlist_id: str, uris: list[str]) -> None:
        """Adiciona faixas a uma playlist em chunks de 100 (limite da API).

        Args:
            playlist_id: ID ou URI da playlist (ex.: "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M")
            uris: Lista de Spotify track URIs (ex.: ["spotify:track:...", ...])

        Levanta exceção se algo falhar — responsabilidade do caller tratar.
        """
        if not uris:
            log.info("playlist.no_tracks_to_add")
            return

        # Parti em chunks de 100
        chunk_size = 100
        chunks = [uris[i : i + chunk_size] for i in range(0, len(uris), chunk_size)]

        for i, chunk in enumerate(chunks, start=1):
            try:
                self.sp.playlist_add_items(playlist_id, chunk)
                log.info(
                    "playlist.chunk_added",
                    chunk=i,
                    total_chunks=len(chunks),
                    chunk_size=len(chunk),
                )
            except Exception as e:
                log.exception(
                    "playlist.add_failed",
                    chunk=i,
                    total_chunks=len(chunks),
                    error=str(e),
                )
                raise

        log.info(
            "playlist.updated",
            playlist_id=playlist_id,
            total_added=len(uris),
            chunks=len(chunks),
        )

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> None:
        """Substitui conteúdo da playlist pelos URIs dados.

        Usa playlist_replace_items para os primeiros 100, depois playlist_add_items
        para os restantes em chunks de 100.

        Args:
            playlist_id: ID ou URI da playlist
            uris: Lista de Spotify track URIs (ordem preservada)

        Levanta exceção se algo falhar — responsabilidade do caller tratar.
        """
        if not uris:
            # Limpa a playlist (replace com lista vazia)
            try:
                self.sp.playlist_replace_items(playlist_id, [])
                log.info("playlist.cleared", playlist_id=playlist_id)
            except Exception as e:
                log.exception("playlist.clear_failed", playlist_id=playlist_id, error=str(e))
                raise
            return

        # Replace com os primeiros 100
        try:
            self.sp.playlist_replace_items(playlist_id, uris[:100])
            log.info(
                "playlist.replaced",
                playlist_id=playlist_id,
                initial_count=min(100, len(uris)),
            )
        except Exception as e:
            log.exception("playlist.replace_failed", playlist_id=playlist_id, error=str(e))
            raise

        # Se há mais de 100, adiciona em chunks
        if len(uris) > 100:
            chunk_size = 100
            remaining_uris = uris[100:]
            chunks = [
                remaining_uris[i : i + chunk_size]
                for i in range(0, len(remaining_uris), chunk_size)
            ]

            for i, chunk in enumerate(chunks, start=1):
                try:
                    self.sp.playlist_add_items(playlist_id, chunk)
                    log.info(
                        "playlist.chunk_added_after_replace",
                        chunk=i,
                        total_chunks=len(chunks),
                        chunk_size=len(chunk),
                    )
                except Exception as e:
                    log.exception(
                        "playlist.add_after_replace_failed",
                        chunk=i,
                        total_chunks=len(chunks),
                        error=str(e),
                    )
                    raise

        log.info(
            "playlist.replaced_complete",
            playlist_id=playlist_id,
            total_items=len(uris),
        )
