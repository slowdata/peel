"""Explicit, feedback-backed selections; preparation has no external effects."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from peel.albums import is_archival_album_title, is_compilation_release
from peel.db import DB
from peel.playlists import canonical_playlist_id
from peel.site_export import _snapshot_album_to_json, spotify_track_url
from peel.sources.registry import source_label


class PublicationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    week: str
    playlist_id: str
    review_playlist_id: str
    track_snapshot_at: str
    album_snapshot_at: str
    tracks: list[str] = Field(min_length=1, max_length=7)
    albums: list[tuple[str, str]] = Field(max_length=7)
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
        if item is None:
            raise ValueError(f"Álbum fora da fila aprovada: {key}")
        feedback = db.album_feedback_for_identity(item.artist, item.album)
        if not feedback or feedback[1] not in {"love", "like"}:
            raise ValueError(f"Álbum sem avaliação positiva: {item.artist} — {item.album}")
        if not is_direct_album_link(item.listen_url, item.listen_kind):
            raise ValueError(f"Álbum sem link directo: {item.album}")
        selected_albums.append(_snapshot_album_to_json(item.model_copy(update={"position": rank})))
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
        and parsed.path.startswith("/album/")
        and bool(parsed.path.removeprefix("/album/"))
    )


@dataclass(frozen=True, slots=True)
class Candidate:
    """A positively rated item, identified by its original queue position."""

    position: int
    key: str | tuple[str, str]
    artist: str
    title: str
    rating: str


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
    return PublicationPlan(
        week=week,
        playlist_id=playlist_id,
        review_playlist_id=review_playlist_id,
        track_snapshot_at=track_header[0],
        album_snapshot_at=album_header[0],
        tracks=[str(track.key) for track in tracks],
        albums=album_keys,
        note=note,
    )
