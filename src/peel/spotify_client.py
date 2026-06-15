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

import re

import spotipy
import structlog
from spotipy.cache_handler import MemoryCacheHandler
from spotipy.oauth2 import SpotifyOAuth

from peel.config import settings

log = structlog.get_logger()


def _clean_for_query(s: str) -> str:
    """Limpa uma string para ser enviada ao Spotify search.

    A API de search do Spotify é sensível a ruído como `(feat. X)`, `[ft. X]`,
    curly quotes e brackets soltos — essas strings viajam literalmente na query
    e reduzem drasticamente o recall. Esta função remove esse ruído mas
    preserva o sinal essencial (nome do artista e título).
    """
    # (feat. X), (ft. X), (Feat. X) — parênteses
    s = re.sub(r"\s*\(\s*f(?:ea)?t\.?\s*[^)]*\)\s*", " ", s, flags=re.IGNORECASE)
    # [feat. X], [ft. X] — brackets
    s = re.sub(r"\s*\[\s*f(?:ea)?t\.?\s*[^\]]*\]\s*", " ", s, flags=re.IGNORECASE)
    # Brackets residuais (ex: "Title [Album Version]")
    s = re.sub(r"\s*\[[^\]]*\]\s*", " ", s)
    # Aspas curly e direitas
    s = re.sub(r"[\u201c\u201d\u2018\u2019\"']", "", s)
    # Separador de colaboração "x" (Shlohmo x SALEM) → &
    s = re.sub(r"\s+x\s+", " & ", s)
    # Whitespace múltiplo
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _and_the_variant(artist: str) -> str | None:
    """Se o artista contém ' And The ', devolve variante com '& The' (ou vice-versa).

    Spotify guarda muitas vezes o nome canónico com '&' (ex: 'Ryan Davis & The
    Roadhouse Band'); blogs escrevem com 'And The'. Serve como fallback quando
    a query primária não devolve nada.
    """
    if re.search(r"\s+and\s+the\s+", artist, flags=re.IGNORECASE):
        return re.sub(r"\s+and\s+the\s+", " & The ", artist, flags=re.IGNORECASE)
    return None


# Scopes necessários para gerir a playlist principal e playlists temporárias.
SCOPES = (
    "playlist-modify-private playlist-modify-public "
    "playlist-read-private playlist-read-collaborative user-read-private"
)

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

        Normalização e matching fuzzy ficam para o matcher.py (Passo 5). A
        string de query é limpa (feat./brackets/quotes) para maximizar recall,
        e se a primeira tentativa for vazia testa-se uma variante `And The` → `&`
        no artista (comum no Spotify).
        """
        clean_artist = _clean_for_query(artist)
        clean_title = _clean_for_query(title)

        # Tentativa primária com query normalizada
        candidates = self._do_search(f"{clean_artist} {clean_title}", limit)
        if candidates:
            return candidates

        # Fallback: variante "And The" → "& The" no artista
        variant = _and_the_variant(clean_artist)
        if variant:
            log.debug("spotify.retry_variant", original=clean_artist, variant=variant)
            candidates = self._do_search(f"{variant} {clean_title}", limit)
            if candidates:
                return candidates

        log.warning("spotify.no_match", artist=artist, title=title)
        return []

    def _do_search(self, query: str, limit: int) -> list[dict]:
        """Executa uma query no Spotify e devolve lista de candidatos."""
        try:
            results = self.sp.search(q=query, type="track", limit=limit)
            items = results.get("tracks", {}).get("items", [])

            if not items:
                return []

            candidates = [
                {
                    "uri": item.get("uri"),
                    "name": item.get("name"),
                    "artists": [a["name"] for a in item.get("artists", [])],
                }
                for item in items
            ]

            log.debug("spotify.search_results", query=query, count=len(candidates))
            return candidates

        except Exception as e:
            log.exception("spotify.search_failed", query=query, error=str(e))
            return []

    def search_album(self, artist: str, album: str, limit: int = 5) -> list[dict]:
        """Procura um álbum no Spotify; devolve candidatos brutos.

        Returns:
            Lista de dicts {"uri", "url", "name", "artists"}. [] se nada/erro.
            O matching fuzzy fica para quem chama (como no search_track).
        """
        clean_artist = _clean_for_query(artist)
        clean_album = _clean_for_query(album)
        try:
            results = self.sp.search(q=f"{clean_artist} {clean_album}", type="album", limit=limit)
            items = results.get("albums", {}).get("items", [])
            return [
                {
                    "uri": item.get("uri"),
                    "url": (item.get("external_urls") or {}).get("spotify"),
                    "name": item.get("name"),
                    "artists": [a["name"] for a in item.get("artists", [])],
                }
                for item in items
            ]
        except Exception as e:
            log.exception("spotify.album_search_failed", artist=artist, album=album, error=str(e))
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
