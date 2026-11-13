"""Explicit, feedback-backed selections; preparation has no external effects."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from peel.albums import is_archival_album_title, is_compilation_release
from peel.db import DB
from peel.matcher import artist_identity_key, normalize
from peel.novelty import NoveltyCheck, spotify_album_id
from peel.playlists import canonical_playlist_id
from peel.site_export import _snapshot_album_to_json, spotify_track_url
from peel.sources.families import source_family
from peel.sources.registry import source_label


class AdditionalAlbum(BaseModel):
    """Explicit album metadata, never inserted into a private listening queue."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    artist: str = Field(min_length=1)
    album: str = Field(min_length=1)
    listen_url: str
    listen_kind: Literal["spotify", "bandcamp"]
    editorial_url: str | None = None
    source_ids: tuple[str, ...] = ()
    metadata_captured_at: str | None = None

    @field_validator("artist", "album")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Nome de artista/álbum vazio")
        return value.strip()

    @field_validator("listen_url")
    @classmethod
    def canonical_listen_url(cls, value: str) -> str:
        parsed = urlparse(value)
        return parsed._replace(path=parsed.path.rstrip("/"), query="", fragment="").geturl()

    @field_validator("editorial_url")
    @classmethod
    def public_editorial_url(cls, value: str | None) -> str | None:
        if value is not None:
            parsed = urlparse(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("URL editorial inválido")
        return value

    @field_validator("metadata_captured_at")
    @classmethod
    def aware_observation(cls, value: str | None) -> str | None:
        if value is not None and datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("Data de observação sem timezone")
        return value

    @property
    def key(self) -> tuple[str, str]:
        return normalize(self.artist), normalize(self.album)


def _album_identity(key: tuple[str, str]) -> tuple[str, str]:
    return artist_identity_key(key[0]), normalize(key[1])


class PublicationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    week: str
    playlist_id: str
    review_playlist_id: str
    track_snapshot_at: str
    album_snapshot_at: str
    tracks: list[str] = Field(min_length=1, max_length=7)
    albums: list[tuple[str, str]] = Field(max_length=7)
    additional_albums: list[AdditionalAlbum] = Field(default_factory=list, max_length=7)
    artist_overrides: dict[str, str] = Field(default_factory=dict)
    note: str = ""


def prepare_publication(db: DB, plan: PublicationPlan) -> dict:
    """Resolve exactly the approved identities/order, never backfill a selection."""
    if db.is_archived_week(plan.week):
        raise ValueError(f"Semana {plan.week} arquivada")
    if len(set(plan.tracks)) != len(plan.tracks) or len(set(plan.albums)) != len(plan.albums):
        raise ValueError("Selecção contém duplicados")
    if set(plan.artist_overrides) - set(plan.tracks) or any(
        not name.strip() for name in plan.artist_overrides.values()
    ):
        raise ValueError("Correcção de artista fora da selecção ou vazia")
    header = db.conn.execute(
        "SELECT confirmed_at FROM review_queue_snapshots WHERE week=? AND playlist_id=?",
        (plan.week, plan.review_playlist_id),
    ).fetchone()
    album_header = db.conn.execute(
        "SELECT created_at FROM album_queue_weeks WHERE week=?", (plan.week,)
    ).fetchone()
    if not header or header[0] != plan.track_snapshot_at:
        raise ValueError("O snapshot de triagem mudou; revê a selecção")
    if not album_header or album_header[0] != plan.album_snapshot_at:
        raise ValueError("O snapshot de álbuns mudou; revê a selecção")
    queue = db.review_queue_snapshot(plan.review_playlist_id, plan.week) or []
    positions = {item.spotify_uri: (n, item) for n, item in enumerate(queue, 1)}
    albums = {(item.artist_key, item.album_key): item for item in db.album_queue(plan.week) or []}
    additions = _validate_additions(db, plan.week, plan.additional_albums)
    if set(additions) - set(plan.albums):
        raise ValueError("Metadados de álbum adicional fora da selecção")
    if len({_album_identity(key) for key in plan.albums}) != len(plan.albums):
        raise ValueError("Selecção contém aliases do mesmo álbum")
    tracks, selected_albums, track_positions, album_positions = [], [], [], []
    for rank, uri in enumerate(plan.tracks, 1):
        if uri not in positions:
            raise ValueError(f"Faixa fora da triagem aprovada: {uri}")
        feedback = db.feedback_for_track_identity(uri)
        if not feedback or feedback[1] not in {"love", "like"}:
            raise ValueError(f"Faixa sem avaliação positiva: {uri}")
        position, item = positions[uri]
        url = spotify_track_url(uri)
        if url is None:
            raise ValueError(f"URI inválida: {uri}")
        tracks.append(
            {
                "rank": rank,
                "artist": plan.artist_overrides.get(uri, item.artist),
                "title": item.title,
                "source": source_label(item.source_id),
                "source_count": item.source_count,
                "spotify_url": url,
                "discovery_week": item.added_at_week,
            }
        )
        track_positions.append(position)
    for rank, key in enumerate(plan.albums, 1):
        item = albums.get(key)
        extra = additions.get(key)
        if item is None and extra is None:
            raise ValueError(f"Álbum fora da fila aprovada: {key}")
        artist, title = (
            (item.artist, item.album) if item is not None else (extra.artist, extra.album)
        )
        feedback = db.album_feedback_for_identity(artist, title)
        if not feedback or feedback[1] not in {"love", "like"}:
            raise ValueError(f"Álbum sem avaliação positiva: {artist} — {title}")
        if extra is not None:
            if extra.metadata_captured_at is None:
                raise ValueError("Metadados adicionais não congelados; prepara a selecção")
            selected_albums.append(_additional_album_to_json(extra, rank))
            album_positions.append(None)  # never invent a private queue position
        else:
            if not is_direct_album_link(item.listen_url, item.listen_kind):
                raise ValueError(f"Álbum sem link directo: {item.album}")
            selected_albums.append(
                _snapshot_album_to_json(item.model_copy(update={"position": rank}))
            )
            album_positions.append(item.position)
    return {
        "week": plan.week,
        "playlist_id": canonical_playlist_id(plan.playlist_id),
        "track_uris": list(plan.tracks),
        "tracks": tracks,
        "albums": selected_albums,
        "track_queue_positions": track_positions,
        "album_queue_positions": album_positions,
        "plan": plan.model_dump(mode="json"),
    }


MAX_PUBLIC_PICKS = 7
_POSITIVE = ("love", "like")


def is_direct_album_link(url: str | None, kind: str | None) -> bool:
    """Only a playable Spotify/Bandcamp album page can be published."""
    parsed = urlparse(url or "")
    host = parsed.hostname or ""
    platform = (kind == "spotify" and host == "open.spotify.com") or (
        kind == "bandcamp" and host.endswith(".bandcamp.com")
    )
    return (
        parsed.scheme == "https"
        and platform
        and parsed.username is None
        and parsed.password is None
        and parsed.port in {None, 443}
        and parsed.path.startswith("/album/")
        and bool(parsed.path.removeprefix("/album/"))
    )


@dataclass(frozen=True, slots=True)
class Candidate:
    """A positive item with a selection code and an explicit queue/outside origin."""

    position: int
    key: str | tuple[str, str]
    artist: str
    title: str
    rating: str
    origin: Literal["queue", "outside_queue"] = "queue"
    listen_url: str | None = None


def _album_link_identity(url: str) -> tuple[str | None, str]:
    parsed = urlparse(url)
    return parsed.hostname, parsed.path.rstrip("/")


def _validate_additions(db: DB, week: str, additions: list[AdditionalAlbum]) -> dict:
    if len(additions) > MAX_PUBLIC_PICKS:
        raise ValueError(f"No máximo {MAX_PUBLIC_PICKS} álbuns adicionais")
    queued = {_album_identity((a.artist_key, a.album_key)) for a in db.album_queue(week) or []}
    seen: set[tuple[str, str]] = set()
    urls = {_album_link_identity(a.listen_url) for a in db.album_queue(week) or [] if a.listen_url}
    result = {}
    for extra in additions:
        identity = _album_identity(extra.key)
        if identity in queued:
            raise ValueError(f"Álbum adicional já pertence à fila: {extra.album}")
        if identity in seen:
            raise ValueError(f"Álbum adicional repetido: {extra.album}")
        if is_archival_album_title(extra.album) or is_compilation_release(
            extra.artist, extra.album
        ):
            raise ValueError(f"Álbum adicional de arquivo/compilação: {extra.album}")
        if not is_direct_album_link(extra.listen_url, extra.listen_kind) or (
            extra.listen_kind == "spotify"
            and not re.fullmatch(r"/album/[A-Za-z0-9]{22}", urlparse(extra.listen_url).path)
        ):
            raise ValueError(f"Álbum adicional sem link directo: {extra.album}")
        if _album_link_identity(extra.listen_url) in urls:
            raise ValueError(f"Link de álbum adicional repetido: {extra.album}")
        seen.add(identity)
        urls.add(_album_link_identity(extra.listen_url))
        result[extra.key] = extra
    return result


def additional_album_candidates(
    db: DB, week: str, additions: list[AdditionalAlbum]
) -> list[Candidate]:
    """Only explicitly supplied, positively rated albums; numbers are UI codes."""
    validated = _validate_additions(db, week, additions)
    start = max((a.position for a in db.album_queue(week) or []), default=0)
    result = []
    for number, extra in enumerate(validated.values(), start + 1):
        feedback = db.album_feedback_for_identity(extra.artist, extra.album)
        if feedback and feedback[1] in _POSITIVE:
            result.append(
                Candidate(
                    number,
                    extra.key,
                    extra.artist,
                    extra.album,
                    feedback[1],
                    "outside_queue",
                    extra.listen_url,
                )
            )
    return result


def _freeze_additional_album(db: DB, extra: AdditionalAlbum) -> AdditionalAlbum:
    if extra.metadata_captured_at is not None:
        return extra  # explicit republication preserves already frozen metadata
    rows = db.conn.execute(
        "SELECT source_id, source_url FROM album_mentions "
        "WHERE artist_identity_key(artist_key)=? AND album_key=? ORDER BY seen_at, source_id",
        _album_identity(extra.key),
    ).fetchall()
    return extra.model_copy(
        update={
            "source_ids": tuple(sorted({str(row[0]) for row in rows})),
            "editorial_url": extra.editorial_url or next((row[1] for row in rows if row[1]), None),
            "metadata_captured_at": datetime.now(UTC).isoformat(),
        }
    )


def _additional_album_to_json(extra: AdditionalAlbum, rank: int) -> dict:
    return {
        "rank": rank,
        "artist": extra.artist,
        "title": extra.album,
        "source": ", ".join(dict.fromkeys(source_label(s) for s in extra.source_ids))
        or "Personal pick",
        "source_count": len({source_family(s) for s in extra.source_ids}),
        "link": extra.editorial_url,
        "listen_url": extra.listen_url,
        "listen_kind": extra.listen_kind,
        "spotify_url": extra.listen_url if extra.listen_kind == "spotify" else None,
        "spotify_match": "direct" if extra.listen_kind == "spotify" else None,
        "selection_origin": "outside_queue",
    }


def verify_additional_releases(
    plan: PublicationPlan,
    previous: PublicationPlan | None,
    *,
    spotify_album: Callable[[str], dict],
    bandcamp_release: Callable,
    reference_date: date,
) -> list[dict]:
    """Live checks before public writes; unchanged published picks are not re-admitted."""
    approved = (
        {(_album_identity(a.key), a.listen_url) for a in previous.additional_albums}
        if previous
        else set()
    )
    pending = [
        a for a in plan.additional_albums if (_album_identity(a.key), a.listen_url) not in approved
    ]
    if any(
        a.listen_kind == "spotify"
        and not re.fullmatch(r"/album/[A-Za-z0-9]{22}", urlparse(a.listen_url).path)
        for a in pending
    ):
        raise ValueError("URL Spotify adicional sem ID de álbum válido")
    expected = {spotify_album_id(a.listen_url): a for a in pending if a.listen_kind == "spotify"}

    def lookup(album_id: str) -> dict:
        extra = expected[album_id]
        data = spotify_album(album_id)
        if not isinstance(data, dict):
            raise ValueError("Metadados Spotify inválidos")
        artists = data.get("artists") or []
        if normalize(str(data.get("name") or "")) != normalize(extra.album) or not any(
            artist_identity_key(str(a.get("name") or "")) == artist_identity_key(extra.artist)
            for a in artists
            if isinstance(a, dict)
        ):
            raise ValueError("A identidade Spotify difere do álbum aprovado")
        return data

    novelty = NoveltyCheck(
        spotify_album=lookup, bandcamp_release=bandcamp_release, reference_date=reference_date
    )
    checked = []
    for extra in pending:
        reason = novelty(extra.artist, extra.album, extra.listen_url, plan.week)
        if reason:
            raise ValueError(
                f"Álbum adicional não elegível: {extra.artist} — {extra.album}: {reason}"
            )
        checked.append(
            {
                "artist_key": extra.key[0],
                "album_key": extra.key[1],
                "listen_url": extra.listen_url,
                "checked_at": datetime.now(UTC).isoformat(),
            }
        )
    return checked


def track_candidates(db: DB, review_playlist_id: str, week: str) -> list[Candidate]:
    """Positive tracks of the confirmed listening queue, in queue order."""
    candidates = []
    for position, item in enumerate(db.review_queue_snapshot(review_playlist_id, week) or [], 1):
        feedback = db.feedback_for_track_identity(item.spotify_uri)
        if feedback and feedback[1] in _POSITIVE:
            candidates.append(
                Candidate(position, item.spotify_uri, item.artist, item.title, feedback[1])
            )
    return candidates


def album_candidates(db: DB, week: str) -> list[Candidate]:
    """Positive, playable, non-archival albums of the private queue."""
    candidates = []
    for item in db.album_queue(week) or []:
        feedback = db.album_feedback_for_identity(item.artist, item.album)
        if (
            feedback
            and feedback[1] in _POSITIVE
            and not is_archival_album_title(item.album)
            and not is_compilation_release(item.artist, item.album)
            and is_direct_album_link(item.listen_url, item.listen_kind)
        ):
            key = (item.artist_key, item.album_key)
            candidates.append(Candidate(item.position, key, item.artist, item.album, feedback[1]))
    return candidates


def default_picks(candidates: list[Candidate], limit: int = MAX_PUBLIC_PICKS) -> list[Candidate]:
    """Loves before likes when there are too many; never pads; keeps queue order."""
    ranked = sorted(candidates, key=lambda c: (_POSITIVE.index(c.rating), c.position))
    return sorted(ranked[:limit], key=lambda c: c.position)


def parse_positions(text: str, candidates: list[Candidate]) -> list[Candidate]:
    """Positions typed by the editor, in the order typed (that is the public order)."""
    by_position = {candidate.position: candidate for candidate in candidates}
    tokens = text.replace(",", " ").split()
    if not tokens:
        return []
    try:
        positions = [int(token.lstrip("#")) for token in tokens]
    except ValueError as exc:
        raise ValueError("Usa números de posição, ex.: 5 13 28") from exc
    if len(set(positions)) != len(positions):
        raise ValueError("Posição repetida")
    if len(positions) > MAX_PUBLIC_PICKS:
        raise ValueError(f"No máximo {MAX_PUBLIC_PICKS}")
    unknown = [position for position in positions if position not in by_position]
    if unknown:
        raise ValueError(f"Sem avaliação positiva nessa posição: {unknown}")
    return [by_position[position] for position in positions]


def build_plan(
    db: DB,
    *,
    week: str,
    playlist_id: str,
    review_playlist_id: str,
    tracks: list[Candidate],
    albums: list[Candidate],
    note: str = "",
    additional_albums: list[AdditionalAlbum] | None = None,
) -> PublicationPlan:
    """Freeze the chosen identities against the exact snapshots that were heard."""
    track_header = db.conn.execute(
        "SELECT confirmed_at FROM review_queue_snapshots WHERE week=? AND playlist_id=?",
        (week, review_playlist_id),
    ).fetchone()
    album_header = db.conn.execute(
        "SELECT created_at FROM album_queue_weeks WHERE week=?", (week,)
    ).fetchone()
    if not track_header or not album_header:
        raise ValueError(f"Sem snapshots confirmados para {week}")
    album_keys = [album.key for album in albums if isinstance(album.key, tuple)]
    supplied = _validate_additions(db, week, additional_albums or [])
    selected_additions = [
        _freeze_additional_album(db, supplied[key]) for key in album_keys if key in supplied
    ]
    return PublicationPlan(
        week=week,
        playlist_id=playlist_id,
        review_playlist_id=review_playlist_id,
        track_snapshot_at=track_header[0],
        album_snapshot_at=album_header[0],
        tracks=[str(track.key) for track in tracks],
        albums=album_keys,
        additional_albums=selected_additions,
        note=note,
    )
