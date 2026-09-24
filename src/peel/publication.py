"""Explicit, feedback-backed selections; preparation has no external effects."""

from __future__ import annotations

from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

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
        parsed = urlparse(item.listen_url or "")
        direct = (
            parsed.scheme == "https"
            and (
                (item.listen_kind == "spotify" and parsed.hostname == "open.spotify.com")
                or (
                    item.listen_kind == "bandcamp"
                    and (parsed.hostname or "").endswith(".bandcamp.com")
                )
            )
            and parsed.path.startswith("/album/")
            and bool(parsed.path.removeprefix("/album/"))
        )
        if not direct:
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
