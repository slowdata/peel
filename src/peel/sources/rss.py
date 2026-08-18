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
from datetime import UTC, datetime, timedelta
from html import unescape
from urllib.parse import urlparse

import feedparser
import httpx
import structlog
from selectolax.parser import HTMLParser, Node

from peel.models import Track
from peel.sources.base import Source

log = structlog.get_logger()


class RSSSource(Source):
    """Classe base para fontes RSS."""

    url: str
    """URL do feed RSS."""

    request_headers: dict[str, str] | None = None
    """Headers HTTP opcionais (ex: User-Agent) para feeds que bloqueiam defaults."""

    max_entries: int | None = None
    """Limite opcional de entries a considerar em feeds grandes."""

    pagination_url_template: str | None = None
    """Template opt-in para páginas secundárias, com ``{page}`` e opcionalmente ``{url}``."""

    lookback_days: int | None = None
    """Janela opcional de antiguidade para fontes RSS paginadas."""

    max_pages: int = 1
    """Máximo de páginas RSS; o default preserva exactamente um pedido."""

    def __init__(self) -> None:
        """Inicializa a fonte (subclasses definem id, name, url)."""
        if not hasattr(self, "id") or not hasattr(self, "name"):
            raise NotImplementedError(f"{self.__class__.__name__} must define id and name")
        if not hasattr(self, "url"):
            raise NotImplementedError(f"{self.__class__.__name__} must define url")

    def fetch(self) -> list[Track]:
        """Parseia o RSS e extrai faixas.

        Por defeito faz exactamente um pedido. Sources que definem um template
        paginado podem avançar até ``max_pages`` e limitar itens à sua janela
        temporal, sem depender da DB.
        """
        tracks: list[Track] = []
        seen_ids: set[str] = set()
        seen_links: set[str] = set()
        total_entries = 0
        considered_entries = 0
        cutoff = self._now() - timedelta(days=self.lookback_days) if self.lookback_days else None
        paginated = self.pagination_url_template is not None and self.max_pages > 1
        page_count = self.max_pages if paginated else 1

        for page in range(1, page_count + 1):
            page_url = self._page_url(page)
            try:
                feed = self._parse_feed(page_url)
            except Exception as e:
                if page == 1:
                    log.exception("rss.fetch_failed", source_id=self.id, url=page_url, error=str(e))
                    raise
                log.warning(
                    "rss.pagination_page_failed",
                    source_id=self.id,
                    page=page,
                    url=page_url,
                    error=str(e),
                )
                log.info(
                    "rss.pagination_stopped",
                    source_id=self.id,
                    page=page,
                    reason="secondary_page_failed",
                )
                break

            if feed.bozo and feed.bozo_exception:
                log.warning(
                    "rss.parse_warning",
                    source_id=self.id,
                    page=page,
                    error=str(feed.bozo_exception),
                )

            page_entries = list(feed.entries)
            total_entries += len(page_entries)
            if not page_entries:
                log.info(
                    "rss.page_fetched",
                    source_id=self.id,
                    page=page,
                    url=page_url,
                    entries=0,
                    deduped_entries=0,
                    considered_entries=0,
                )
                if paginated:
                    log.info(
                        "rss.pagination_stopped",
                        source_id=self.id,
                        page=page,
                        reason="empty_page",
                    )
                break

            unique_entries: list[dict] = []
            deduped_entries = 0
            for entry in page_entries:
                entry_id, entry_link = self._entry_identifiers(entry)
                is_duplicate = (entry_id is not None and entry_id in seen_ids) or (
                    entry_link is not None and entry_link in seen_links
                )
                if entry_id is not None:
                    seen_ids.add(entry_id)
                if entry_link is not None:
                    seen_links.add(entry_link)
                if is_duplicate:
                    deduped_entries += 1
                    continue
                unique_entries.append(entry)

            entries = unique_entries
            if self.max_entries is not None:
                entries = entries[: self.max_entries]
            if cutoff is not None:
                entries = [
                    entry
                    for entry in entries
                    if (published_at := self._entry_published_at(entry)) is None
                    or published_at >= cutoff
                ]
            considered_entries += len(entries)

            log.info(
                "rss.page_fetched",
                source_id=self.id,
                page=page,
                url=page_url,
                entries=len(page_entries),
                deduped_entries=deduped_entries,
                considered_entries=len(entries),
            )
            for entry in entries:
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

            if (
                cutoff is not None
                and unique_entries
                and all(
                    (published_at := self._entry_published_at(entry)) is not None
                    and published_at < cutoff
                    for entry in unique_entries
                )
            ):
                log.info(
                    "rss.pagination_stopped",
                    source_id=self.id,
                    page=page,
                    reason="lookback_exhausted",
                    lookback_days=self.lookback_days,
                )
                break

            if page == page_count and paginated:
                log.info(
                    "rss.pagination_stopped",
                    source_id=self.id,
                    page=page,
                    reason="max_pages",
                )

        log.info(
            "rss.fetched",
            source_id=self.id,
            total_entries=total_entries,
            considered_entries=considered_entries,
            valid_tracks=len(tracks),
        )
        return tracks

    def _parse_feed(self, url: str):
        if self.request_headers:
            return feedparser.parse(url, request_headers=self.request_headers)
        return feedparser.parse(url)

    def _page_url(self, page: int) -> str:
        if page == 1 or self.pagination_url_template is None:
            return self.url
        return self.pagination_url_template.format(url=self.url, page=page)

    def _now(self) -> datetime:
        """Relógio isolado para tornar o lookback determinístico em testes."""
        return datetime.now(UTC)

    @staticmethod
    def _entry_identifiers(entry: dict) -> tuple[str | None, str | None]:
        """Devolve ID e link independentemente para dedupe entre páginas."""
        identifier = str(entry.get("id") or entry.get("guid") or "").strip() or None
        link = str(entry.get("link") or "").strip() or None
        return identifier, link

    @staticmethod
    def _entry_published_at(entry: dict) -> datetime | None:
        parsed_time = entry.get("published_parsed") or entry.get("updated_parsed")
        if not parsed_time:
            return None
        try:
            return datetime(*parsed_time[:6], tzinfo=UTC)
        except (TypeError, ValueError):
            return None

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

        # Extrai published_at timezone-aware se disponível.
        published_at = self._entry_published_at(entry)

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


_PITCHFORK_NON_CURRENT_RE = re.compile(
    r"\b(?:reissue|reissued|remaster(?:ed)?|deluxe|expanded|anniversary|archival|archive|"
    r"retrospective|box set)\b",
    re.IGNORECASE,
)


def _canonical_feed_link(value: str) -> str:
    try:
        parsed = urlparse(value)
    except ValueError:
        return value.rstrip("/")
    return parsed._replace(query="", fragment="").geturl().rstrip("/")


class PitchforkAlbumReviews(PitchforkBestAlbums):
    """Current regular Pitchfork album reviews, excluding BNA and archives.

    Best New Albums remains a stronger independent source.  Fetching its small
    feed first lets this source remove same-publication overlap rather than
    manufacturing consensus.
    """

    id = "pitchfork_album_reviews"
    name = "Pitchfork Album Reviews"
    url = "https://pitchfork.com/feed/reviews/albums/rss"
    best_url = "https://pitchfork.com/feed/reviews/best/albums/rss"
    lookback_days = 8

    def __init__(self) -> None:
        super().__init__()
        self._best_links: set[str] = set()

    def fetch(self) -> list[Track]:
        best_feed = self._parse_feed(self.best_url)
        if not best_feed.entries:
            raise RuntimeError("Pitchfork BNA feed empty; refusing duplicate reviews")
        self._best_links = {
            _canonical_feed_link(str(entry.get("link", "")))
            for entry in best_feed.entries
            if entry.get("link")
        }
        return super().fetch()

    def _extract_artist_title(self, entry: dict) -> tuple[str, str] | None:
        link = str(entry.get("link", "")).strip()
        if not link or _canonical_feed_link(link) in self._best_links:
            return None
        context = " ".join(
            str(entry.get(field, "")) for field in ("title", "summary", "description", "author")
        )
        context = _strip_html_tags(context)
        if "each sunday" in context.lower() or _PITCHFORK_NON_CURRENT_RE.search(context):
            return None
        return super()._extract_artist_title(entry)


class GuardianMusicAlbums(RSSSource):
    """The Guardian Music — album reviews.

    Feed: https://www.theguardian.com/music/rss

    O feed mistura notícias, entrevistas, live reviews e críticas. Para evitar ruído,
    esta source só extrai títulos no formato de crítica de álbum:

    ``Artist: Album review ...``

    Produz items `kind = "album"`: entram no relatório, não na playlist.
    """

    id = "guardian_music_albums"
    name = "The Guardian Music — Album Reviews"
    url = "https://www.theguardian.com/music/rss"
    kind = "album"

    def _extract_artist_title(self, entry: dict) -> tuple[str, str] | None:
        title = _strip_html_tags(entry.get("title", "").strip())
        if not title:
            return None

        match = re.match(r"^(?P<artist>[^:]+):\s+(?P<album>.+?)\s+review\b", title)
        if not match:
            return None

        artist = match.group("artist").strip()
        album = match.group("album").strip()
        if not artist or not album:
            return None
        return artist, album


_RELEASE_NEWS_EXCLUDED_KEYWORDS = (
    " cover",
    " remix",
    " live",
    " tour",
    " festival",
    " performance",
    " anniversary",
    " reissue",
    " remaster",
    " deluxe",
    " instrumental",
    " sped up",
    " slowed",
)


def _clean_news_title(title: str) -> str:
    """Normaliza títulos RSS de notícias para parsing conservador."""
    text = _strip_html_tags(unescape(title))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _title_without_quoted_segments(title: str) -> str:
    """Remove títulos entre aspas para filtros de ruído contextuais."""
    return re.sub(r"[\"\u201c][^\"\u201d]*[\"\u201d]", " ", title)


def _clean_news_artist(artist: str) -> str:
    """Remove qualificadores editoriais antes do nome do artista."""
    artist = re.sub(
        r"^[\"'\u2018\u201c][^\"'\u2019\u201d]+[\"'\u2019\u201d]\s+",
        "",
        artist,
    ).strip()
    artist = re.sub(r"^watch\s+", "", artist, flags=re.IGNORECASE).strip()
    artist = re.sub(r"^newcomers\s+", "", artist, flags=re.IGNORECASE).strip()
    if re.search(r"\s+newcomers\s+", artist, flags=re.IGNORECASE):
        artist = re.split(r"\s+newcomers\s+", artist, flags=re.IGNORECASE, maxsplit=1)[1]
    return artist.strip(" ,-:;\u2013\u2014")


def _clean_news_track(track: str) -> str:
    """Remove ruído comum após o nome da faixa."""
    track = track.strip().strip("\"'\u2018\u2019\u201c\u201d")
    track = re.sub(r"\s+(?:following|via|after|from|off)\b.*$", "", track, flags=re.IGNORECASE)
    track = re.sub(r"\s*:\s*listen\b.*$", "", track, flags=re.IGNORECASE)
    return track.strip(" ,-:;\u2013\u2014")


def _has_release_news_signal(title: str) -> bool:
    lower = f" {title.lower()} "
    unquoted_lower = f" {_title_without_quoted_segments(title).lower()} "
    if any(keyword in unquoted_lower for keyword in _RELEASE_NEWS_EXCLUDED_KEYWORDS):
        return False
    return any(
        signal in lower
        for signal in (
            " new song",
            " new single",
            " new track",
            " shares ",
            " share ",
            " releases ",
            " release ",
            " returns with",
            " return with",
            " are back with",
            " is back with",
            " announces ",
            " announce ",
            " introduces ",
            " introduce ",
            " reveals ",
            " reveal ",
            " unleash ",
            " unleashes ",
            " unleashed ",
            " hear ",
            " listen ",
        )
    )


def _extract_release_news_artist_title(title: str) -> tuple[str, str] | None:
    """Extrai artist/track de títulos narrativos de release news.

    Mantém-se conservador: exige sinais claros de música nova e, salvo padrão
    específico de "return with new single", uma faixa entre aspas.
    """
    title = _clean_news_title(title)
    if not title or not _has_release_news_signal(title):
        return None

    unquoted_lower = _title_without_quoted_segments(title).lower()
    has_explicit_new_track = re.search(r"\bnew\s+(?:song|single|track)\b", unquoted_lower)
    if re.search(r"\bvideo\s+for\b", unquoted_lower) and not has_explicit_new_track:
        return None
    if re.search(r"\b(?:new\s+)?version\s+of\b", unquoted_lower) and not has_explicit_new_track:
        return None

    quoted_patterns = [
        # Nigel Godrich and Dhani Harrison Form Dragonflies, Release “Slower” Single
        # Only the newly formed band is the artist, not its founders.
        (
            r"^(?:.+?)\s+Form\s+(?P<artist>[^,;:]+),\s+"
            r"(?:Release|Releases|Released)\s+[\"\u201c](?P<track>[^\"\u201d]+)[\"\u201d]"
        ),
        # Y U QT Sign to Ninja Tune and Share New Single “Call My Name”
        # Bound the artist before signing context instead of treating the whole
        # clause before "share" as its name.
        (
            r"^(?P<artist>.+?)\s+(?:Sign|Signs|Signed)\s+to\b.*?\band\s+"
            r"(?:Share|Shares|Shared|Release|Releases|Released|Unveil|Unveils|Unveiled)"
            r"\b.*?[\"\u201c](?P<track>[^\"\u201d]+)[\"\u201d]"
        ),
        # Protomartyr Announce New Album Hotel Usona, Share “Sounds We Cannot Hear”
        # Twin Temple Announce LP Doomed Lovers and Share “Haunt Me”
        # Keep the artist bounded by the announcement verb; comma, semicolon and
        # "and" all separate album context from the named release.
        (
            r"^(?P<artist>.+?)\s+(?:Announce|Announces|Announced|"
            r"Confirm|Confirms|Confirmed|Detail|Details|Detailed|"
            r"Reveal|Reveals|Revealed|Unveil|Unveils|Unveiled)\s+"
            r".*?\b(?:Album|LP|EP)\b[^,;:]*(?:[,;]\s*|\s+and\s+)"
            r"(?:Share|Shares|Shared|Release|Releases|Released|Unveil|Unveils|Unveiled|"
            r"Unleash|Unleashes|Unleashed)\b"
            r"(?:\s+(?:(?:their|a|the)\s+)?"
            r"(?:(?:brand-new|new|lead|latest|title|debut)\s+)*"
            r"(?:song|single|track))?,?\s+"
            r"[\"\u201c](?P<track>[^\"\u201d]+)[\"\u201d]"
        ),
        # Listen/Hear to the New Aphex Twin Song “Example”
        (
            r"^(?:Listen|Hear)\s+to\s+the\s+New\s+(?P<artist>.+?)\s+"
            r"(?:Song|Single|Track)\s+[\"\u201c](?P<track>[^\"\u201d]+)[\"\u201d]"
        ),
        # Listen/Hear to The Strokes’ New Song “Falling Out of Love”
        (
            r"^(?:Listen|Hear)\s+to\s+(?P<artist>.+?)(?:[\u2019']s?)?\s+"
            r"(?:(?:New\s+)?(?:Song|Single|Track)|Duet)\s+"
            r"[\"\u201c](?P<track>[^\"\u201d]+)[\"\u201d]"
        ),
        # Rico Nasty Announces New Album RX: Hear “Cupcake”
        (
            r"^(?P<artist>.+?)\s+(?:Announce|Announces|Announced|"
            r"Introduce|Introduces|Introduced|Reveal|Reveals|Revealed|"
            r"Unveil|Unveils|Unveiled|Detail|Details|Detailed)\b.*?"
            r"\b(?:Song|Single|Track|Hear|Listen|With)\b.*?"
            r"[\"\u201c](?P<track>[^\"\u201d]+)[\"\u201d]"
        ),
        # The Strokes Share New Single “Falling Out Of Love”: Listen
        (
            r"^(?P<artist>.+?)\s+(?:Share|Shares|Shared|Release|Releases|Released|"
            r"Unleash|Unleashes|Unleashed|Drop|Drops|Dropped|Return|Returns|Returned|"
            r"Surprise-Release|Surprise-Releases|Surprise-Released)\b.*?"
            r"[\"\u201c](?P<track>[^\"\u201d]+)[\"\u201d]"
        ),
        # Captain Crocodile are back with a new single, “Fragmented Tool”
        (
            r"^(?P<artist>.+?)\s+(?:is|are)\s+back\s+with\b.*?"
            r"[\"\u201c](?P<track>[^\"\u201d]+)[\"\u201d]"
        ),
        # Carly Rae ... on New Song “On Wires”
        (
            r"^(?P<artist>.+?)\s+Wants\b.*?New\s+Song\b.*?"
            r"[\"\u201c](?P<track>[^\"\u201d]+)[\"\u201d]"
        ),
        # Watch Charli XCX ... New Song “Wink Wink”
        (
            r"^Watch\s+(?P<artist>.+?)\s+(?:Let|Perform|Share|Release|Debut|Play)\b"
            r".*?New\s+Song\b.*?"
            r"[\"\u201c](?P<track>[^\"\u201d]+)[\"\u201d]"
        ),
    ]
    for pattern in quoted_patterns:
        match = re.match(pattern, title, flags=re.IGNORECASE)
        if not match:
            continue
        artist = _clean_news_artist(match.group("artist"))
        track = _clean_news_track(match.group("track"))
        if artist and track:
            return artist, track

    # Cigarettes After Sex return with new single, Twizzler
    match = re.match(
        r"^(?P<artist>.+?)\s+(?:return|returns|returned)\s+with\s+"
        r"(?:a\s+)?(?:(?:\w+)\s+){0,2}single,?\s+(?P<track>[^,:;\u2013\u2014]+)$",
        title,
        flags=re.IGNORECASE,
    )
    if match:
        artist = _clean_news_artist(match.group("artist"))
        track = _clean_news_track(match.group("track"))
        if artist and track:
            return artist, track

    return None


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

        if match:
            artist = match.group("artist").strip()
            track_title = match.group("track").strip()
            if artist and track_title:
                return artist, track_title

        # Fallback conservador para posts narrativos em /music/:
        # "The Strokes Share New Single “Falling Out Of Love”: Listen".
        # Evita /news/ para não apanhar covers live, tours, performances, etc.
        link = entry.get("link", "")
        if "/music/" in link:
            narrative = _extract_release_news_artist_title(title)
            if narrative is not None:
                return narrative

        # Narrativa, não é track review
        log.warning(
            "stereogum.title_no_match",
            title=title,
        )
        return None


class PitchforkNews(RSSSource):
    """Pitchfork News — release news filtradas.

    Feed amplo, por isso só aceitamos títulos com sinais fortes de faixa nova
    (new song/single, share/release/listen/hear) e extraction conservadora.
    """

    id = "pitchfork_news"
    name = "Pitchfork News"
    url = "https://www.pitchfork.com/feed/feed-news/rss"

    def _extract_artist_title(self, entry: dict) -> tuple[str, str] | None:
        title = entry.get("title", "").strip()
        result = _extract_release_news_artist_title(title)
        if result is None:
            log.warning("pitchfork_news.title_no_match", title=title)
        return result


_LINEOFBESTFIT_ARTIST_PROSE = re.compile(
    r"\b(?:duo|trio|quartet|band|outfit|collective|artist|singer-songwriter|"
    r"newcomers?|sign(?:s|ed)?|announce(?:s|d)?|confirm(?:s|ed)?|"
    r"reveal(?:s|ed)?|detail(?:s|ed)?)\b",
    re.IGNORECASE,
)


def _lineofbestfit_summary_artist(
    entry: dict,
    headline_artist: str,
    track: str,
) -> str:
    """Prefer a shorter feed-summary subject when it clarifies headline prose."""
    summary = _clean_news_title(str(entry.get("summary", "")))
    if not summary:
        return headline_artist

    match = re.match(
        r"^(?P<artist>.+?)\s+(?:has|have)\s+"
        r"(?:announced|released|shared|returned|unveiled|revealed|confirmed|signed|introduced)\b",
        summary,
        flags=re.IGNORECASE,
    )
    if not match:
        return headline_artist

    summary_artist = _clean_news_artist(match.group("artist"))
    headline_key = _fold_news_text(headline_artist)
    summary_key = _fold_news_text(summary_artist)
    track_key = _fold_news_text(track)
    full_summary_key = _fold_news_text(summary)
    if not summary_key or not track_key or not _contains_news_phrase(full_summary_key, track_key):
        return headline_artist
    if (
        len(summary_key) < len(headline_key)
        and _LINEOFBESTFIT_ARTIST_PROSE.search(headline_artist)
        and _contains_news_phrase(headline_key, summary_key)
    ):
        return summary_artist
    return headline_artist


def _fold_news_text(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def _contains_news_phrase(text: str, phrase: str) -> bool:
    return f" {phrase} " in f" {text} "


class LineOfBestFitNews(RSSSource):
    """The Line of Best Fit — news de novos singles.

    Fonte boa para indie/alt-pop; fica como track source com filtro agressivo
    para evitar anúncios sem faixa.
    """

    id = "lineofbestfit_news"
    name = "The Line of Best Fit — News"
    url = "https://feeds.feedburner.com/thelineofbestfit"

    def _extract_artist_title(self, entry: dict) -> tuple[str, str] | None:
        title = entry.get("title", "").strip()
        result = _extract_release_news_artist_title(title)
        if result is None:
            log.warning("lineofbestfit.title_no_match", title=title)
            return None
        artist, track = result
        return _lineofbestfit_summary_artist(entry, artist, track), track


_CONSEQUENCE_NON_RELEASE_TOPICS = re.compile(
    r"\b(?:interview|obituary|film|cinema|television|listicle|roundup|playlist)\b",
    re.IGNORECASE,
)


class ConsequenceMusic(RSSSource):
    """Consequence — Music release news, paginada e estritamente etiquetada."""

    id = "consequence_music"
    name = "Consequence Music"
    kind = "track"
    url = "https://consequence.net/category/music/feed/"
    pagination_url_template = "{url}?paged={page}"
    lookback_days = 8
    max_pages = 16

    def _parse_entry(self, entry: dict) -> Track | None:
        terms = {str(tag.get("term", "")).strip() for tag in entry.get("tags", [])}
        if not {"Music", "New Music Releases"}.issubset(terms):
            return None
        return super()._parse_entry(entry)

    def _extract_artist_title(self, entry: dict) -> tuple[str, str] | None:
        title = entry.get("title", "").strip()
        if _CONSEQUENCE_NON_RELEASE_TOPICS.search(_title_without_quoted_segments(title)):
            return None
        result = _extract_release_news_artist_title(title)
        if result is None:
            log.warning("consequence_music.title_no_match", title=title)
        return result


class KexpInOurHeadphones(RSSSource):
    """KEXP — In Our Headphones.

    O antigo Song of the Day migrou para um podcast de descoberta no mesmo feed.
    Cada episódio é curado por DJs/músicos e costuma destacar uma faixa concreta
    dentro da descrição. A extraction é conservadora: exige um título de faixa
    entre aspas e um artista recuperável do título do episódio ou da descrição.
    """

    id = "kexp_in_our_headphones"
    name = "KEXP — In Our Headphones"
    url = (
        "https://www.omnycontent.com/d/playlist/"
        "bad5d079-8dcb-4630-8770-aa090049131d/"
        "32b2ac38-5a48-4300-9fa6-aa40002038b5/"
        "4ac1c451-4315-4096-ab9b-aa40002038c4/podcast.rss"
    )
    max_entries = 50

    def _extract_artist_title(self, entry: dict) -> tuple[str, str] | None:
        episode_title = _clean_kexp_text(entry.get("title", ""))
        body = _clean_kexp_text(entry.get("summary") or entry.get("description") or "")

        direct = self._extract_direct_episode_title(episode_title)
        if direct is not None:
            return direct

        legacy = self._extract_legacy_song_of_the_day(body)
        if legacy is not None:
            return legacy

        hyphen = self._extract_hyphen_episode_title(episode_title)
        if hyphen is not None:
            return hyphen

        track_title = self._extract_track_title(body)
        artist = self._extract_artist_from_episode_title(episode_title) or self._extract_artist(
            body
        )
        if artist and track_title:
            return artist, track_title

        return None

    def _extract_direct_episode_title(self, title: str) -> tuple[str, str] | None:
        match = re.search(r"(?P<artist>.+?)[\u2019']s\s+[\"\u201c](?P<track>[^\"\u201d]+)", title)
        if match is None:
            return None
        artist_phrase = match.group("artist")
        if " on " in artist_phrase and " and " in artist_phrase:
            artist_phrase = artist_phrase.rsplit(" and ", maxsplit=1)[1]
        artist = _clean_kexp_entity(artist_phrase)
        track = _clean_kexp_entity(match.group("track"))
        if artist and track:
            return artist, track
        return None

    def _extract_hyphen_episode_title(self, title: str) -> tuple[str, str] | None:
        match = re.match(r"(?P<artist>.+?)\s+-\s+(?P<track>.+)$", title)
        if match is None:
            return None
        artist = _clean_kexp_entity(match.group("artist"))
        track = _clean_kexp_entity(match.group("track"))
        if artist and track:
            return artist, track
        return None

    def _extract_legacy_song_of_the_day(self, body: str) -> tuple[str, str] | None:
        match = re.search(
            r"\bis\s+[\"\u201c](?P<track>[^\"\u201d]+)[\"\u201d]\s+by\s+"
            r"(?P<artist>.+?)(?:,|\s+from\b|\.)",
            body,
            flags=re.IGNORECASE,
        )
        if match is None:
            return None
        artist = _clean_kexp_entity(match.group("artist"))
        track = _clean_kexp_entity(match.group("track"))
        if artist and track:
            return artist, track
        return None

    def _extract_track_title(self, body: str) -> str | None:
        patterns = [
            r"(?:song|track)\s+[\"\u201c](?P<track>[^\"\u201d]+)[\"\u201d]",
            r"[\"\u201c](?P<track>[^\"\u201d]+)[\"\u201d]\s+(?:comes|is)\s+from\b",
            r"the\s+stunning\s+[\"\u201c](?P<track>[^\"\u201d]+)[\"\u201d]",
        ]
        for pattern in patterns:
            match = re.search(pattern, body, flags=re.IGNORECASE)
            if match is None:
                continue
            track = _clean_kexp_entity(match.group("track"))
            if track and track.lower() not in {"in our headphones", "what's in our headphones"}:
                return track
        return None

    def _extract_artist_from_episode_title(self, title: str) -> str | None:
        descriptor = (
            r"(?:Post[- ]Punks?|Darkwave Band|Punks?|Band|Group|Rapper|Artist|"
            r"Musician|Singer|Songwriter)"
        )
        patterns = [
            r"\bof\s+(?P<artist>.+)$",
            r"\band\s+(?P<artist>[A-Z][A-Za-z0-9& .'-]+?)[\u2019']s\b",
            r"\bon\s+(?P<artist>[A-Z][A-Za-z0-9& .'-]+?)[\u2019']s\b",
            rf"\b{descriptor}\s+(?P<artist>(?:(?!\bon\b).)+)$",
            r"\bBand\s+(?P<artist>.+?)\s+and\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, title)
            if match is None:
                continue
            artist = _clean_kexp_episode_artist(match.group("artist"))
            if artist:
                return artist
        return None

    def _extract_artist(self, body: str) -> str | None:
        match = re.search(
            r"\b(?:new\s+)?(?:track|song|music)\s+from\s+(.+?)(?:\.|,|\s+and\b)",
            body,
            flags=re.IGNORECASE,
        )
        if match is None:
            return None
        return _clean_kexp_artist_phrase(match.group(1))


# User-Agent de browser usado por feeds que bloqueiam defaults (ex: Quietus/Cloudflare)
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _strip_html_tags(s: str) -> str:
    """Remove tags HTML simples (ex: <i>, <b>, <em>) de um título."""
    return re.sub(r"<[^>]+>", "", s)


def _clean_kexp_text(value: str) -> str:
    text = _strip_html_tags(unescape(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _clean_kexp_entity(value: str) -> str:
    return value.strip().strip("\"'\u2018\u2019\u201c\u201d .,:;-").strip()


def _clean_kexp_artist_phrase(value: str) -> str | None:
    text = _clean_kexp_entity(value)
    text = re.sub(r"^the\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\([^)]*\)\s+", "", text).strip()
    return _clean_kexp_episode_artist(text)


def _clean_kexp_episode_artist(value: str) -> str | None:
    text = _clean_kexp_entity(value)
    prefixes = [
        r"(?:Bay Area|Oakland|Dutch|New York|Seattle|Los Angeles|LA|L\.A\.|UK|"
        r"British|Australian|Australia[\u2019']s|Chicago|Detroit)",
        r"(?:post[- ]punks?|post[- ]punk|darkwave|punks?|punk|rapper|band|"
        r"group|duo|artist|musician|singer|songwriter)",
    ]
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            new_text = re.sub(rf"^{prefix}\s+", "", text, flags=re.IGNORECASE).strip()
            if new_text != text:
                text = new_text
                changed = True
    return _clean_kexp_entity(text) or None


def _clean_quietus_chart_text(value: str) -> str:
    """Limpa texto dos chart-items da Quietus (aspas curly e espaços)."""
    return value.strip().strip("\u2018\u2019\u201c\u201d'\"").strip()


def _clean_npr_text(value: str) -> str:
    """Limpa texto extraído de HTML NPR."""
    text = _strip_html_tags(unescape(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip("\u2018\u2019\u201c\u201d'\"").strip()


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


class AquariumDrunkard(Source):
    """Aquarium Drunkard — On The Turntable.

    Página: https://aquariumdrunkard.com/

    A homepage tem um bloco editorial pequeno e server-rendered, ``On The
    Turntable``, com álbuns em rotação. É uma fonte mais adequada do que o RSS
    geral da AD: curada, curta e explicitamente album-oriented. Produz items
    ``kind = "album"`` para digest/report, sem Spotify matching nem playlist.

    Estratégia: parsear ``ul.on_the_turntable_content li.album``, separar o
    ``h3`` no primeiro ``::``, usar o link ``Read More`` como ``source_url`` e
    converter ``a.spotify-link`` em ``spotify:album:<id>`` quando existe. Itens
    malformados são saltados com warning; falha HTTP levanta exceção para o
    orquestrador tratar por source.
    """

    id = "aquarium_drunkard"
    name = "Aquarium Drunkard — On The Turntable"
    kind = "album"
    url = "https://aquariumdrunkard.com/"
    request_headers = {"User-Agent": _BROWSER_UA}

    def fetch(self) -> list[Track]:
        """Extrai os álbuns do bloco On The Turntable da homepage."""
        response = httpx.get(
            self.url,
            headers=self.request_headers,
            follow_redirects=True,
            timeout=20,
        )
        response.raise_for_status()
        return self._parse_homepage_html(response.text)

    def _parse_homepage_html(self, html: str) -> list[Track]:
        parser = HTMLParser(html)
        items = parser.css("div.turntable-items ul.on_the_turntable_content li.album")
        if not items:
            log.warning("aquariumdrunkard.turntable_not_found", url=self.url)
            return []

        albums: list[Track] = []
        for item in items:
            album = self._parse_album_item(item)
            if album is not None:
                albums.append(album)
        return albums

    def _parse_album_item(self, item: Node) -> Track | None:
        heading = item.css_first("div.album-content h3") or item.css_first("h3")
        if heading is None:
            log.warning("aquariumdrunkard.album_heading_missing")
            return None

        raw_title = re.sub(r"\s+", " ", heading.text(strip=True)).strip()
        parsed = self._split_turntable_title(raw_title)
        if parsed is None:
            log.warning("aquariumdrunkard.album_title_no_match", title=raw_title)
            return None

        artist, album_title = parsed
        if not album_title:
            log.warning("aquariumdrunkard.empty_album", title=raw_title)
            return None

        source_url = self._read_more_url(item)
        if source_url is None:
            # Decisão: manter o álbum mesmo sem link. O link é útil no digest,
            # mas `Track.source_url` é opcional e a curadoria continua válida.
            log.warning("aquariumdrunkard.read_more_missing", title=raw_title)
        if self._is_archival_item(item):
            log.info("aquariumdrunkard.archival_skipped", title=raw_title)
            return None

        return Track(
            source_id=self.id,
            artist=artist,
            title=album_title,
            source_url=source_url,
            published_at=self._published_at_from_url(source_url),
            raw_title=raw_title,
            spotify_album_uri=self._spotify_album_uri(item),
        )

    def _split_turntable_title(self, title: str) -> tuple[str, str] | None:
        match = re.match(r"^(?P<artist>.+?)\s*::\s*(?P<album>.*)$", title)
        if match is None:
            return None

        artist = self._clean_turntable_text(match.group("artist"))
        album = self._clean_turntable_text(match.group("album"))
        if not artist:
            return None
        return artist, album

    def _read_more_url(self, item: Node) -> str | None:
        for link in item.css("div.description a"):
            href = link.attributes.get("href", "").strip()
            text = link.text(strip=True).lower()
            if href and "read more" in text:
                return href
        return None

    def _published_at_from_url(self, source_url: str | None) -> datetime | None:
        if not source_url:
            return None
        try:
            path = urlparse(source_url).path
        except ValueError:
            return None
        match = re.search(r"/(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/", path)
        if match is None:
            return None
        try:
            return datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
                tzinfo=UTC,
            )
        except ValueError:
            return None

    def _is_archival_item(self, item: Node) -> bool:
        description = item.css_first("div.description")
        text = description.text(separator=" ", strip=True) if description else ""
        return bool(
            re.search(
                r"\b(?:originally released|reissue|archival|archive release|"
                r"anniversary|expanded edition|lost album)\b",
                text,
                flags=re.IGNORECASE,
            )
        )

    def _spotify_album_uri(self, item: Node) -> str | None:
        link = item.css_first("a.spotify-link")
        if link is None:
            return None
        href = link.attributes.get("href", "").strip()
        if not href:
            return None
        if href.startswith("spotify:album:"):
            return href

        parsed = urlparse(href)
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.netloc == "open.spotify.com" and len(parts) >= 2 and parts[0] == "album":
            return f"spotify:album:{parts[1]}"
        log.warning("aquariumdrunkard.spotify_album_url_invalid", href=href)
        return None

    def _clean_turntable_text(self, value: str) -> str:
        text = _strip_html_tags(unescape(value))
        return re.sub(r"\s+", " ", text).strip().strip("\u2018\u2019\u201c\u201d'\"").strip()


class NprNewMusicFridayStarting5(Source):
    """NPR New Music Friday — The Starting 5.

    A NPR publica artigos semanais "New Music Friday" com várias secções. Esta
    source extrai apenas ``The Starting 5``: cinco músicas de alta curadoria.

    Produz items ``kind = "track"``: entram no Spotify matching, playlist,
    feedback e scoring como qualquer outra source de faixas.
    """

    id = "npr_new_music_friday_starting5"
    name = "NPR New Music Friday — The Starting 5"
    kind = "track"
    section_url = "https://www.npr.org/sections/allsongs/606254804/new-music-friday"
    request_headers = {"User-Agent": _BROWSER_UA}

    def fetch(self) -> list[Track]:
        """Procura o artigo New Music Friday mais recente e extrai The Starting 5."""
        section_response = httpx.get(
            self.section_url,
            headers=self.request_headers,
            follow_redirects=True,
            timeout=20,
        )
        section_response.raise_for_status()
        article_url = self._latest_article_url(section_response.text)
        if article_url is None:
            log.warning("npr_nmf.article_not_found", section_url=self.section_url)
            return []

        article_response = httpx.get(
            article_url,
            headers=self.request_headers,
            follow_redirects=True,
            timeout=20,
        )
        article_response.raise_for_status()
        return self._parse_article_html(article_response.text, article_url)

    def _latest_article_url(self, html: str) -> str | None:
        parser = HTMLParser(html)
        for link in parser.css("a"):
            href = link.attributes.get("href", "").strip()
            if "/new-music-friday-best-albums-" in href:
                return href
        return None

    def _parse_article_html(self, html: str, source_url: str) -> list[Track]:
        story = HTMLParser(html).css_first("#storytext")
        if story is None:
            log.warning("npr_nmf.storytext_not_found", source_url=source_url)
            return []

        published_at = self._published_at(html)
        in_starting_5 = False
        tracks: list[Track] = []
        for node in story.iter():
            if node.tag == "h2":
                heading = _clean_npr_text(node.text(strip=True))
                if heading == "The Starting 5":
                    in_starting_5 = True
                    continue
                if in_starting_5:
                    break

            if not in_starting_5 or node.tag != "p":
                continue

            parsed = self._parse_starting_5_paragraph(node.html)
            if parsed is None:
                continue
            artist, title = parsed
            tracks.append(
                Track(
                    source_id=self.id,
                    artist=artist,
                    title=title,
                    source_url=source_url,
                    published_at=published_at,
                    raw_title=f"{artist} — {title}",
                )
            )

        return tracks

    def _parse_starting_5_paragraph(self, html: str) -> tuple[str, str] | None:
        match = re.search(
            r"<p>\s*🎵\s*(?P<artist>.*?),\s*<em>(?P<title>.*?)</em>",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match is None:
            return None

        artist = _clean_npr_text(match.group("artist"))
        title = _clean_npr_text(match.group("title"))
        if not artist or not title:
            return None
        return artist, title

    def _published_at(self, html: str) -> datetime | None:
        parser = HTMLParser(html)
        time_node = parser.css_first("time[datetime]")
        if time_node is None:
            return None
        value = time_node.attributes.get("datetime", "").strip()
        if not value or value.startswith("P"):
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None


class TheQuietusTracksOfMonth(Source):
    """The Quietus — Music of the Month / TRACKS.

    Esta source é separada de ``TheQuietus``: a Quietus principal fica como
    álbum/contexto, enquanto esta extrai apenas a secção ``TRACKS`` dos artigos
    mensais "Music of the Month: The Best Albums and Tracks...".
    """

    id = "thequietus_tracks_of_month"
    name = "The Quietus — Tracks of the Month"
    kind = "track"
    feed_url = "https://thequietus.com/feed/"
    fallback_chart_url = (
        "https://thequietus.com/tq-charts/music-of-the-month/"
        "music-of-the-month-the-best-albums-and-tracks-of-april-2026/"
    )
    request_headers = {"User-Agent": _BROWSER_UA}

    def fetch(self) -> list[Track]:
        """Procura o artigo mensal mais recente no feed e extrai a secção TRACKS."""
        chart_url, published_at = self._latest_chart_entry()
        response = httpx.get(
            chart_url,
            headers=self.request_headers,
            follow_redirects=True,
            timeout=20,
        )
        response.raise_for_status()
        return self._parse_chart_html(response.text, chart_url, published_at)

    def _latest_chart_entry(self) -> tuple[str, datetime | None]:
        feed = feedparser.parse(self.feed_url, request_headers=self.request_headers)
        for entry in feed.entries:
            link = entry.get("link", "").strip()
            title = entry.get("title", "").strip().lower()
            if "/tq-charts/music-of-the-month/" not in link:
                continue
            if "tracks" not in title:
                continue

            published_at = None
            if entry.get("published"):
                try:
                    parsed_time = entry.published_parsed
                    if parsed_time:
                        published_at = datetime(*parsed_time[:6])
                except Exception:
                    pass
            return link, published_at

        return self.fallback_chart_url, None

    def _parse_chart_html(
        self,
        html: str,
        source_url: str,
        published_at: datetime | None = None,
    ) -> list[Track]:
        """Extrai apenas chart-items que aparecem depois do heading TRACKS."""
        section = self._tracks_section_html(html)
        if section is None:
            log.warning("quietus_tracks.section_not_found", source_url=source_url)
            return []

        parser = HTMLParser(section)
        tracks: list[Track] = []
        for item in parser.css("div.chart-item"):
            parsed = self._parse_chart_item(item)
            if parsed is None:
                continue
            artist, title = parsed
            tracks.append(
                Track(
                    source_id=self.id,
                    artist=artist,
                    title=title,
                    source_url=source_url,
                    published_at=published_at,
                    raw_title=f"{artist} — {title}",
                )
            )
        return tracks

    def _tracks_section_html(self, html: str) -> str | None:
        marker = re.search(
            r"<h2[^>]*>\s*<strong>\s*TRACKS\s*</strong>\s*</h2>",
            html,
            flags=re.IGNORECASE,
        )
        if marker is None:
            return None

        tail = html[marker.end() :]
        end = re.search(r"<h2[^>]*>\s*From the Archive", tail, flags=re.IGNORECASE)
        if end is not None:
            return tail[: end.start()]
        return tail

    def _parse_chart_item(self, item) -> tuple[str, str] | None:
        header = item.css_first(".chart-entry-header h2") or item.css_first("h2")
        if header is None:
            return None

        title_node = header.css_first("em")
        if title_node is None:
            return None

        artists = [_clean_quietus_chart_text(node.text(strip=True)) for node in header.css("a")]
        artists = [artist for artist in artists if artist]
        title = _clean_quietus_chart_text(title_node.text(strip=True))

        if not artists or not title:
            return None
        return ", ".join(artists), title


_QUIETUS_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


class TheQuietusFeedbacker(Source):
    """Strict album extraction from the latest Quietus Feedbacker/Rock column."""

    id = "thequietus_feedbacker"
    name = "The Quietus — Feedbacker"
    kind = "album"
    listing_url = "https://thequietus.com/columns/quietus-reviews/rock/"
    request_headers = {"User-Agent": _BROWSER_UA}
    max_age_days = 8

    def fetch(self) -> list[Track]:
        listing = httpx.get(
            self.listing_url,
            headers=self.request_headers,
            follow_redirects=True,
            timeout=20,
        )
        listing.raise_for_status()
        article_url = self._latest_article_url(listing.text)
        if article_url is None:
            log.warning("quietus_feedbacker.article_not_found", url=self.listing_url)
            return []
        article = httpx.get(
            article_url,
            headers=self.request_headers,
            follow_redirects=True,
            timeout=20,
        )
        article.raise_for_status()
        published_at = self._published_at(article.text)
        if published_at is None:
            log.warning("quietus_feedbacker.date_not_found", url=article_url)
            return []
        if published_at < self._now() - timedelta(days=self.max_age_days):
            return []
        return self._parse_article_html(article.text, article_url, published_at)

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _latest_article_url(self, html: str) -> str | None:
        parser = HTMLParser(html)
        for link in parser.css("a"):
            href = link.attributes.get("href", "").strip()
            text = re.sub(r"\s+", " ", link.text(strip=True)).strip().lower()
            try:
                parsed = urlparse(href)
            except ValueError:
                continue
            if (
                parsed.hostname == "thequietus.com"
                and parsed.path.startswith("/quietus-reviews/rock/")
                and text.startswith("feedbacker:")
            ):
                return href
        return None

    def _published_at(self, html: str) -> datetime | None:
        parser = HTMLParser(html)
        node = parser.css_first('time[itemprop="datePublished"]')
        value = node.attributes.get("datetime", "") if node else ""
        match = re.search(
            r"(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]+),\s+(?P<year>\d{4})",
            value,
        )
        if match is None:
            return None
        month = _QUIETUS_MONTHS.get(match.group("month").lower())
        if month is None:
            return None
        try:
            return datetime(int(match.group("year")), month, int(match.group("day")), tzinfo=UTC)
        except ValueError:
            return None

    def _parse_article_html(
        self,
        html: str,
        source_url: str,
        published_at: datetime,
    ) -> list[Track]:
        parser = HTMLParser(html)
        albums: list[Track] = []
        seen: set[tuple[str, str]] = set()
        for heading in parser.css("h2"):
            artist_node = heading.css_first("a")
            album_node = heading.css_first("em")
            label_node = heading.css_first("span.label")
            if artist_node is None or album_node is None or label_node is None:
                continue
            artist = re.sub(r"\s+", " ", artist_node.text(strip=True)).strip()
            album = re.sub(r"\s+", " ", album_node.text(strip=True)).strip()
            key = (artist.casefold(), album.casefold())
            if not artist or not album or key in seen or _PITCHFORK_NON_CURRENT_RE.search(album):
                continue
            seen.add(key)
            albums.append(
                Track(
                    source_id=self.id,
                    artist=artist,
                    title=album,
                    source_url=source_url,
                    published_at=published_at,
                    raw_title=f"{artist} — {album}",
                )
            )
        return albums


class TheQuietus(RSSSource):
    """The Quietus — reviews de álbuns/contexto.

    Feed: https://thequietus.com/feed/
    Bloqueia User-Agents não-browser (retorna 403), por isso passamos um UA
    de Chrome via request_headers.

    Estratégia de filtro (alta precisão, baixo recall — preferimos sinal):
    - Apenas processamos URLs de review directa: /quietus-reviews/<slug>-review/
    - Ignoramos paths aninhados (/quietus-reviews/metal/..., /reissue-of-the-week/,
      /live-reviews/, /album-of-the-week/) que tipicamente são listicles, reissues
      ou reviews não-musicais (livros).
    - Ignoramos news, interviews, culture, opinion — onde extrair tracks é ruidoso.

    Título format: 'Artist – Album Title' (en-dash ou em-dash).
    """

    id = "thequietus"
    name = "The Quietus"
    url = "https://thequietus.com/feed/"
    kind = "album"
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
        return segments[1].endswith("-review")


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
