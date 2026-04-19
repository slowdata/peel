"""Fontes baseadas em RSS feed (Pitchfork, Stereogum, KEXP, etc.).

Decisão: classe genérica RSSSource que parseia RSS e extrai artist/title.
Razão: a maioria dos feeds tem padrão similar (título, link, date).
Subclasses apenas definem ID, nome e URL.

Parse de título é delicado — Pitchfork usa "Artist: 'Track Title'",
mas pode ser diferente noutros feeds. Cada subclasse pode override
_parse_entry() se precisa de lógica especial.
"""

from __future__ import annotations

import re
from datetime import datetime

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

        Pode ser overridden por subclasses para lógica específica.
        Default: assume título em formato "Artist: 'Track Title'" ou "Artist - Track Title".
        """
        title = entry.get("title", "").strip()
        if not title:
            return None

        artist, track_title = self._split_artist_title(title)
        if not artist or not track_title:
            log.warning(
                "rss.could_not_parse_title",
                source_id=self.id,
                title=title,
            )
            return None

        # Extrai published_at se disponível
        published_at = None
        if entry.get("published"):
            try:
                # feedparser converte para struct_time; convertemos a datetime
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

    def _split_artist_title(self, s: str) -> tuple[str, str]:
        """Extrai artist e title de uma string.

        Tenta padrões comuns:
        - "Artist: 'Track Title'"
        - "Artist – 'Track Title'"
        - "Artist - Track Title"
        - "Artist • Track Title"

        Retorna (artist, title) ou ("", "") se não conseguir.
        """
        # Padrão 1: "Artist: 'Track Title'" (com aspas)
        match = re.match(r"([^:]+?):\s*['\"]([^'\"]+)['\"]", s)
        if match:
            return match.group(1).strip(), match.group(2).strip()

        # Padrão 2: "Artist – 'Track Title'" ou "Artist: Track Title" (sem aspas)
        for sep in [":", "–", "—", " - ", " • "]:
            if sep in s:
                parts = s.split(sep, 1)
                if len(parts) == 2:
                    artist = parts[0].strip()
                    title = parts[1].strip()
                    # Remove aspas se estiverem presentes
                    title = re.sub(r"^['\"]|['\"]$", "", title)
                    if artist and title:
                        return artist, title

        # Nenhum padrão casou
        return "", ""


class PitchforkBNT(RSSSource):
    """Pitchfork — Best New Tracks.

    Feed: https://pitchfork.com/rss/reviews/best/tracks/
    Título típico: "The Smile: 'A Light for Attracting Attention'"
    """

    id = "pitchfork_bnt"
    name = "Pitchfork Best New Tracks"
    url = "https://pitchfork.com/rss/reviews/best/tracks/"
