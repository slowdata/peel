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


class PitchforkBNT(RSSSource):
    """Pitchfork — Reviews / Tracks.

    Feed: https://pitchfork.com/feed/rss (feed geral, não só tracks)
    Filtro: apenas entries com category == "Reviews / Tracks"

    URL format: https://pitchfork.com/reviews/tracks/<artist-slug>-<title-slug>/
    Título: entre aspas curly ("..." ou "...")

    Estratégia de extraction:
    1. Filtrar por category == "Reviews / Tracks"
    2. Title vem no entry.title, remove aspas curly
    3. Artist extrai de entry.link:
       - Extrai o slug completo (último segmento do path)
       - Slugifica o título
       - Subtrai o title-slug do artist-slug
       - Converte hyphens em espaços + title-case
    """

    id = "pitchfork_bnt"
    name = "Pitchfork Reviews / Tracks"
    url = "https://pitchfork.com/feed/rss"

    def _parse_entry(self, entry: dict) -> Track | None:
        """Override para filtrar apenas "Reviews / Tracks"."""
        category = entry.get("category", "").strip()
        if category != "Reviews / Tracks":
            return None

        # Chama parent (que vai chamar _extract_artist_title)
        return super()._parse_entry(entry)

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

            # Slugifica o título
            title_slug = self._slugify(title)

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

    def _slugify(self, s: str) -> str:
        """Converte string em slug.

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

    def _slug_to_titlecase(self, slug: str) -> str:
        """Converte slug (artist-name) em title case."""
        # Substitui hyphens por espaços e aplica title case
        return slug.replace("-", " ").title()
