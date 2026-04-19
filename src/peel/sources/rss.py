"""Fontes baseadas em RSS feed (Pitchfork, Stereogum, KEXP, etc.).

Decisão: classe genérica RSSSource que parseia RSS mas deixa extraction de
artist/title para subclasses (cada feed tem seu formato).

Subclasses:
- Definem id, name, url
- Implementam _extract_artist_title(entry) -> tuple[str, str] | None
  (retorna (artist, title) ou None se não conseguir extrair)
"""

from __future__ import annotations

import re
from abc import abstractmethod
from datetime import datetime
from urllib.parse import urlparse

import feedparser
import structlog

from peel.models import Track
from peel.sources.base import Source

log = structlog.get_logger()


class RSSSource(Source):
    """Classe base para fontes RSS."""

    url: str
    """URL do feed RSS."""

    request_headers: dict[str, str] | None = None
    """Headers HTTP opcionais (ex: User-Agent) para feeds que bloqueiam defaults."""

    def __init__(self) -> None:
        """Inicializa a fonte (subclasses definem id, name, url)."""
        if not hasattr(self, "id") or not hasattr(self, "name"):
            raise NotImplementedError(f"{self.__class__.__name__} must define id and name")
        if not hasattr(self, "url"):
            raise NotImplementedError(f"{self.__class__.__name__} must define url")

    def fetch(self) -> list[Track]:
        """Parseia o RSS e extrai faixas.

        Retorna lista de Track. Se uma entry falhar parse, loga warning e salta.
        Se o feed todo falhar (HTTP erro, XML inválido), levanta exceção.
        """
        try:
            if self.request_headers:
                feed = feedparser.parse(self.url, request_headers=self.request_headers)
            else:
                feed = feedparser.parse(self.url)
        except Exception as e:
            log.exception(
                "rss.fetch_failed",
                source_id=self.id,
                url=self.url,
                error=str(e),
            )
            raise

        # Valida que o feed foi parseado
        if feed.bozo and feed.bozo_exception:
            log.warning(
                "rss.parse_warning",
                source_id=self.id,
                error=str(feed.bozo_exception),
            )

        tracks = []
        for entry in feed.entries:
            try:
                track = self._parse_entry(entry)
                if track:
                    tracks.append(track)
            except Exception as e:
                log.warning(
                    "rss.entry_parse_failed",
                    source_id=self.id,
                    entry_title=entry.get("title", "unknown"),
                    error=str(e),
                )
                continue

        log.info(
            "rss.fetched",
            source_id=self.id,
            total_entries=len(feed.entries),
            valid_tracks=len(tracks),
        )

        return tracks

    def _parse_entry(self, entry: dict) -> Track | None:
        """Parseia uma entry RSS e devolve Track ou None.

        Extrai artist/title chamando self._extract_artist_title() que é
        implementado por subclasses.
        """
        title = entry.get("title", "").strip()
        if not title:
            return None

        # Subclasses implementam a extraction
        result = self._extract_artist_title(entry)
        if result is None:
            return None

        artist, track_title = result

        # Extrai published_at se disponível
        published_at = None
        if entry.get("published"):
            try:
                parsed_time = entry.published_parsed
                if parsed_time:
                    published_at = datetime(*parsed_time[:6])
            except Exception:
                pass

        return Track(
            source_id=self.id,
            artist=artist,
            title=track_title,
            source_url=entry.get("link"),
            published_at=published_at,
            raw_title=title,
        )

    @abstractmethod
    def _extract_artist_title(self, entry: dict) -> tuple[str, str] | None:
        """Extrai artist e title de uma entry RSS.

        Implementado por cada subclasse conforme seu formato.

        Returns:
            (artist, title) ou None se não conseguir extrair.
        """
        ...


def _slugify_pitchfork(s: str) -> str:
    """Converte string em slug (helper partilhado entre classes Pitchfork).

    Lowercase, remove aspas/curly-quotes, substitui não-alfanuméricos por hyphen,
    colapsa hyphens repetidos.
    """
    s = s.lower()

    # Remove aspas (retas e curly)
    s = re.sub(r'["\'\'\"]', "", s)

    # Substitui não-alfanuméricos por hyphen
    s = re.sub(r"[^a-z0-9]+", "-", s)

    # Colapsa hyphens repetidos
    s = re.sub(r"-+", "-", s)

    # Remove hyphens no início/fim
    s = s.strip("-")

    return s


class PitchforkBNT(RSSSource):
    """Pitchfork Best New Tracks.

    Feed: https://pitchfork.com/feed/reviews/best/tracks/rss

    Todas as entries são BNT — sem filtro de category necessário.

    URL format: https://pitchfork.com/reviews/tracks/<artist-slug>-<title-slug>/
    Título: entre aspas curly ("..." ou "...")

    Estratégia de extraction:
    1. Title vem no entry.title, remove aspas curly
    2. Artist extrai de entry.link:
       - Extrai o slug completo (último segmento do path)
       - Slugifica o título
       - Subtrai o title-slug do artist-slug
       - Converte hyphens em espaços + title-case
    """

    id = "pitchfork_bnt"
    name = "Pitchfork Best New Tracks"
    url = "https://pitchfork.com/feed/reviews/best/tracks/rss"

    def _extract_artist_title(self, entry: dict) -> tuple[str, str] | None:
        """Extrai artist e title do entry Pitchfork."""
        # Título: vem no entry.title entre aspas (retas ou curly)
        title = entry.get("title", "").strip()
        if not title:
            return None

        # Remove aspas no início e fim (retas " ', ou curly " ")
        title = re.sub(r'^["\'\u201c\u201d]|["\'\u201c\u201d]$', "", title).strip()
        if not title:
            return None

        # Artist: extrai da URL
        link = entry.get("link", "").strip()
        if not link:
            log.warning(
                "pitchfork.no_link",
                title=title,
            )
            return None

        artist = self._extract_artist_from_link(link, title)
        if not artist:
            return None

        return artist, title

    def _extract_artist_from_link(self, link: str, title: str) -> str | None:
        """Extrai artist da URL usando o title como referência.

        URL format: https://pitchfork.com/reviews/tracks/<artist-slug>-<title-slug>/

        Estratégia:
        1. Extrai o slug completo (último segmento não-vazio do path)
        2. Slugifica o título
        3. Se slug-completo termina com "-" + title-slug, remove para obter artist-slug
        4. Converte artist-slug (hyphens) em title-case
        5. Se a subtracção não bater, retorna None (e loga warning)
        """
        try:
            parsed = urlparse(link)
            # Path típico: /reviews/tracks/artist-slug-title-slug/
            path = parsed.path.rstrip("/")
            segments = [s for s in path.split("/") if s]

            if not segments:
                return None

            # Slug completo é o último segmento
            full_slug = segments[-1]

            # Slugifica o título (usa função helper de módulo)
            title_slug = _slugify_pitchfork(title)

            # Tenta remover o title-slug do final
            if full_slug.endswith(f"-{title_slug}"):
                artist_slug = full_slug[: -(len(title_slug) + 1)]
            else:
                # Slug divergiu — não adivinhamos
                log.warning(
                    "pitchfork.slug_mismatch",
                    full_slug=full_slug,
                    title_slug=title_slug,
                    title=title,
                    link=link,
                )
                return None

            # Converte slug para title-case
            artist = self._slug_to_titlecase(artist_slug)
            return artist if artist else None

        except Exception as e:
            log.warning(
                "pitchfork.artist_extraction_failed",
                link=link,
                title=title,
                error=str(e),
            )
            return None

    def _slug_to_titlecase(self, slug: str) -> str:
        """Converte slug (artist-name) em title case."""
        # Substitui hyphens por espaços e aplica title case
        return slug.replace("-", " ").title()


class PitchforkBestAlbums(RSSSource):
    """Pitchfork Best New Albums.

    Feed: https://pitchfork.com/feed/reviews/best/albums/rss

    IMPORTANTE: O Peel trabalha com tracks, não álbuns. Esta source produz
    Track objects onde o `title` é o NOME DO ÁLBUM. A conversão álbum→tracks
    para a playlist é decidida downstream (em main.py) — esta classe só
    estrutura o input.

    URL format: https://pitchfork.com/reviews/albums/<artist-slug>-<album-slug>/
    Título: nome do álbum (sem aspas)

    Estratégia de extraction (idêntica à PitchforkBNT):
    1. Title vem no entry.title (já sem aspas)
    2. Artist extrai de entry.link usando subtracção de slug
       - Extrai o slug completo (último segmento do path)
       - Slugifica o título (album name)
       - Subtrai o album-slug do artist-slug
       - Converte hyphens em espaços + title-case

    DECISÃO: _slugify_pitchfork foi extraída para função de módulo-level
    para evitar duplicação entre PitchforkBNT e PitchforkBestAlbums.
    """

    id = "pitchfork_best_albums"
    name = "Pitchfork Best New Albums"
    url = "https://pitchfork.com/feed/reviews/best/albums/rss"
    kind = "album"

    def _extract_artist_title(self, entry: dict) -> tuple[str, str] | None:
        """Extrai artist e album name do entry Pitchfork."""
        # Título: nome do álbum (sem aspas, tal como vem)
        title = entry.get("title", "").strip()
        if not title:
            return None

        # Artist: extrai da URL
        link = entry.get("link", "").strip()
        if not link:
            log.warning(
                "pitchfork_albums.no_link",
                title=title,
            )
            return None

        artist = self._extract_artist_from_link(link, title)
        if not artist:
            return None

        return artist, title

    def _extract_artist_from_link(self, link: str, album_title: str) -> str | None:
        """Extrai artist da URL usando o album name como referência.

        URL format: https://pitchfork.com/reviews/albums/<artist-slug>-<album-slug>/

        Estratégia: idêntica à PitchforkBNT mas com album-slug em vez de title-slug.
        """
        try:
            parsed = urlparse(link)
            # Path típico: /reviews/albums/artist-slug-album-slug/
            path = parsed.path.rstrip("/")
            segments = [s for s in path.split("/") if s]

            if not segments:
                return None

            # Slug completo é o último segmento
            full_slug = segments[-1]

            # Slugifica o album name (usa função helper de módulo)
            album_slug = _slugify_pitchfork(album_title)

            # Tenta remover o album-slug do final
            if full_slug.endswith(f"-{album_slug}"):
                artist_slug = full_slug[: -(len(album_slug) + 1)]
            else:
                # Slug divergiu — não adivinhamos
                log.warning(
                    "pitchfork_albums.slug_mismatch",
                    full_slug=full_slug,
                    album_slug=album_slug,
                    album_title=album_title,
                    link=link,
                )
                return None

            # Converte slug para title-case
            artist = self._slug_to_titlecase(artist_slug)
            return artist if artist else None

        except Exception as e:
            log.warning(
                "pitchfork_albums.artist_extraction_failed",
                link=link,
                album_title=album_title,
                error=str(e),
            )
            return None

    def _slug_to_titlecase(self, slug: str) -> str:
        """Converte slug (artist-name) em title case."""
        return slug.replace("-", " ").title()


class StereogumNewMusic(RSSSource):
    """Stereogum — New Music.

    Feed: https://www.stereogum.com/feed/
    Filtro: apenas entries com tag "New Music"

    Título format: Artist – "Track Title" (optional features)
    - Dash pode ser em-dash (U+2013), em-dash (U+2014), ou ASCII hyphen
    - Quotes podem ser curly (U+201C/U+201D) ou straight ASCII "

    Estratégia de extraction:
    1. Filtrar por tag "New Music"
    2. Usar regex para extrair Artist e Title (primeiro track citado se múltiplos)
    3. Se não match o padrão, é narrativa — retorna None com warning
    """

    id = "stereogum_new_music"
    name = "Stereogum — New Music"
    url = "https://www.stereogum.com/feed/"

    def _parse_entry(self, entry: dict) -> Track | None:
        """Override para filtrar apenas "New Music"."""
        # Verifica se tem tags
        tags = entry.get("tags", [])
        if not tags:
            return None

        # Procura por "New Music" na lista de tags
        has_new_music = any(t.get("term") == "New Music" for t in tags)
        if not has_new_music:
            return None

        # Chama parent (que vai chamar _extract_artist_title)
        return super()._parse_entry(entry)

    def _extract_artist_title(self, entry: dict) -> tuple[str, str] | None:
        """Extrai artist e title do entry Stereogum.

        Padrão esperado: Artist – "Track Title"
        - Dash: em-dash (–, U+2013), em-dash (—, U+2014), ou ASCII hyphen (-)
        - Quotes: curly (" ", U+201C/U+201D) ou straight (")
        - Se múltiplas tracks citadas (e.g. "Track A" & "Track B"), pega só a primeira

        TRADE-OFF: Posts com múltiplas tracks (e.g., 'Artist – "A" & "B"')
        retornam apenas a primeira. Justificativa: a maioria dos posts é single-track,
        e representar só a primeira permite incluir estes posts úteis. Alternativa seria
        skip completo (mais conservador, mas perde valor).
        """
        title = entry.get("title", "").strip()
        if not title:
            return None

        # Regex para extrair: Artist – "Title"
        # Suporta: em-dash (–), em-dash (—), ASCII hyphen (-)
        # Suporta: curly quotes (" ") ou straight quotes (")
        pattern = r'^(?P<artist>.+?)\s+[–—-]\s+["\u201c"](?P<track>[^"\u201c\u201d]+?)["\u201d"]'
        match = re.match(pattern, title)

        if not match:
            # Narrativa, não é track review
            log.warning(
                "stereogum.title_no_match",
                title=title,
            )
            return None

        artist = match.group("artist").strip()
        track_title = match.group("track").strip()

        if not artist or not track_title:
            return None

        return artist, track_title


# User-Agent de browser usado por feeds que bloqueiam defaults (ex: Quietus/Cloudflare)
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _strip_html_tags(s: str) -> str:
    """Remove tags HTML simples (ex: <i>, <b>, <em>) de um título."""
    return re.sub(r"<[^>]+>", "", s)


def _split_artist_title_dash(title: str) -> tuple[str, str] | None:
    """Separa um título no formato 'Artist – Title' em tuplo.

    Aceita en-dash (U+2013) e em-dash (U+2014) como separadores. NÃO aceita
    hyphen ASCII para evitar falsos positivos com títulos que contêm hyphens
    (ex: 'X-Files', 'Lo-Fi').
    """
    pattern = r"^(?P<artist>.+?)\s+[–—]\s+(?P<title>.+)$"
    match = re.match(pattern, title)
    if not match:
        return None
    artist = match.group("artist").strip()
    track = match.group("title").strip()
    if not artist or not track:
        return None
    return artist, track


class TheQuietus(RSSSource):
    """The Quietus — reviews de tracks/álbuns.

    Feed: https://thequietus.com/feed/
    Bloqueia User-Agents não-browser (retorna 403), por isso passamos um UA
    de Chrome via request_headers.

    Estratégia de filtro (alta precisão, baixo recall — preferimos sinal):
    - Apenas processamos URLs de review directa: /quietus-reviews/<slug>-review/
    - Ignoramos paths aninhados (/quietus-reviews/metal/..., /reissue-of-the-week/,
      /live-reviews/, /album-of-the-week/) que tipicamente são listicles, reissues
      ou reviews não-musicais (livros).
    - Ignoramos news, interviews, culture, opinion — onde extrair tracks é ruidoso.

    Título format: 'Artist – Track/Album Title' (en-dash ou em-dash).
    """

    id = "thequietus"
    name = "The Quietus"
    url = "https://thequietus.com/feed/"
    request_headers = {"User-Agent": _BROWSER_UA}

    def _extract_artist_title(self, entry: dict) -> tuple[str, str] | None:
        """Extrai artist/title se a URL for de review directa."""
        link = entry.get("link", "").strip()
        if not link or not self._is_direct_review(link):
            return None

        title = _strip_html_tags(entry.get("title", "").strip())
        if not title:
            return None

        result = _split_artist_title_dash(title)
        if result is None:
            log.warning("quietus.title_no_match", title=title, link=link)
            return None

        return result

    def _is_direct_review(self, link: str) -> bool:
        """True se o path for /quietus-reviews/<slug>-review/ (não aninhado)."""
        try:
            parsed = urlparse(link)
        except Exception:
            return False
        segments = [s for s in parsed.path.split("/") if s]
        if len(segments) != 2:
            return False
        if segments[0] != "quietus-reviews":
            return False
        if not segments[1].endswith("-review"):
            return False
        return True


class GorillaVsBear(RSSSource):
    """Gorilla vs. Bear — indie electrónico, hip hop, leftfield.

    Feed: https://www.gorillavsbear.net/feed/

    Formato do título: 'Artist – Track' (en-dash), com variantes:
    - Álbuns têm <i>Title</i> (removemos tags HTML antes do parse)
    - Features vêm como '(feat. X)' no título — mantemos no track title
    - Posts não-musicais a filtrar:
      * Listas anuais ('Gorilla vs. Bear\\'s Songs of 2025')
      * Fotos ao vivo ('photos: Artist – live in X')
      * Reviews ao vivo (track title começa com 'live ')
    """

    id = "gorillavsbear"
    name = "Gorilla vs. Bear"
    url = "https://www.gorillavsbear.net/feed/"

    def _extract_artist_title(self, entry: dict) -> tuple[str, str] | None:
        """Extrai artist/title, filtrando ruído conhecido."""
        raw = entry.get("title", "").strip()
        if not raw:
            return None

        title = _strip_html_tags(raw)

        # Listas editoriais com o nome da publicação
        if title.lower().startswith("gorilla vs. bear"):
            return None

        # Fotos/live reviews — prefixo 'photos:' claro
        if re.match(r"^photos?\s*:", title, re.IGNORECASE):
            return None

        result = _split_artist_title_dash(title)
        if result is None:
            log.warning("gvb.title_no_match", title=title)
            return None

        artist, track = result

        # Filtros extra no track title: reviews de concertos
        if re.match(r"^live\b", track, re.IGNORECASE):
            return None

        return artist, track
