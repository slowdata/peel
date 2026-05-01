"""CLI do Peel.

Backoffice local para:
- correr o pipeline semanal,
- ver estado da DB,
- listar tracks recentes,
- dar feedback,
- fazer diagnóstico básico do ambiente.
"""

from __future__ import annotations

import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from peel.config import settings
from peel.db import DB, FEEDBACK_RATINGS
from peel.main import run as run_pipeline

PROJECT_ROOT = Path(__file__).resolve().parents[2]
console = Console(width=120)
app = typer.Typer(add_completion=False, help="Peel — música curada, sincronizada e visível.")


@dataclass(slots=True)
class TrackSummary:
    spotify_uri: str
    artist: str
    title: str
    added_at_week: str
    source_count: int
    first_added_at: str
    last_added_at: str


@dataclass(slots=True)
class SourceState:
    source_id: str
    last_run_at: str
    last_status: str
    last_error: str | None


FeedbackRow = tuple[str, str, str, str, int, str, str]


@app.command("run")
def run_command() -> None:
    """Executa a pipeline semanal do Peel."""
    run_pipeline()


@app.command()
def status() -> None:
    """Mostra estado da DB e das últimas runs."""
    db_path = _resolve_path(settings.db_path)
    exists = db_path.exists()

    status_table = Table(title="Peel status", show_header=False)
    status_table.add_column("Campo", style="bold")
    status_table.add_column("Valor")
    status_table.add_row("DB path", str(db_path))
    status_table.add_row("DB exists", "yes" if exists else "no")

    unique_tracks = 0
    source_rows = 0
    unmatched = 0
    source_states: list[SourceState] = []

    if exists:
        with sqlite3.connect(db_path) as conn:
            unique_tracks = _scalar(
                conn,
                "SELECT COUNT(DISTINCT spotify_uri) FROM tracks",
            )
            source_rows = _scalar(conn, "SELECT COUNT(*) FROM sources_state")
            unmatched = _scalar(
                conn,
                "SELECT COUNT(*) FROM (SELECT DISTINCT source_id, artist, title FROM unmatched)",
            )
            source_states = _fetch_source_states(conn)

    status_table.add_row("Unique tracks", str(unique_tracks))
    status_table.add_row("Sources", str(source_rows))
    status_table.add_row("Unmatched", str(unmatched))

    console.print(Panel(status_table, title="Peel"))

    if source_states:
        table = Table(title="Source state")
        table.add_column("Source")
        table.add_column("Last run")
        table.add_column("Status")
        table.add_column("Error")
        for row in source_states:
            table.add_row(
                row.source_id,
                row.last_run_at,
                row.last_status,
                row.last_error or "",
            )
        console.print(table)


@app.command()
def tracks(
    sources: bool = typer.Option(False, "--sources", help="Mostra as fontes por música"),
    limit: int = typer.Option(20, "--limit", min=1, help="Número máximo de músicas"),
) -> None:
    """Lista músicas recentes com fontes agregadas."""
    db_path = _resolve_path(settings.db_path)
    if not db_path.exists():
        console.print(f"DB não existe: {db_path}")
        return

    with sqlite3.connect(db_path) as conn:
        rows = _fetch_track_summaries(conn, limit)
        if not rows:
            console.print("Sem tracks registadas ainda.")
            return

        table = Table(title="Recent tracks")
        table.add_column("Artist", style="bold")
        table.add_column("Title")
        table.add_column("Week")
        table.add_column("Sources", justify="right")
        table.add_column("Rating")
        table.add_column("Added first")
        table.add_column("Added last")

        for row in rows:
            feedback = _feedback_for_track(conn, row.spotify_uri)
            table.add_row(
                row.artist,
                row.title,
                row.added_at_week,
                str(row.source_count),
                feedback[1] if feedback else "",
                row.first_added_at,
                row.last_added_at,
            )

        console.print(table)

        if sources:
            console.print()
            for row in rows:
                _print_track_sources(conn, row)


@app.command()
def feedback(
    uri: str | None = typer.Option(None, "--uri", help="Spotify URI a avaliar"),
    rating: str | None = typer.Option(None, "--rating", help="love|like|meh|skip|ban"),
    comment: str | None = typer.Option(None, "--comment", help="Comentário opcional"),
    week: str | None = typer.Option(None, "--week", help="Semana ISO a avaliar"),
    limit: int = typer.Option(20, "--limit", min=1, help="Número máximo de tracks"),
) -> None:
    """Regista feedback explícito ou entra em modo interactivo."""
    db_path = _resolve_path(settings.db_path)
    db = DB(str(db_path))
    try:
        db.init_schema()

        if uri is not None:
            chosen_rating = rating or _prompt_rating(default="like")
            _save_feedback(db, uri, chosen_rating, comment)
            console.print(f"Saved feedback: {uri} -> {chosen_rating}")
            return

        rows = db.unrated_tracks(week=week, limit=limit)
        if not rows:
            console.print("Sem tracks por avaliar.")
            return

        for index, row in enumerate(rows, start=1):
            _print_feedback_prompt(db, row, index, len(rows))
            chosen_rating = _prompt_rating(default="like")
            if chosen_rating in {"q", "quit", "exit"}:
                break
            chosen_comment = _prompt_comment(default="")
            _save_feedback(db, row[0], chosen_rating, chosen_comment)
            console.print(f"Saved: {row[1]} — {row[2]} [{chosen_rating}]")
    finally:
        db.close()


@app.command()
def doctor() -> None:
    """Valida o ambiente local do Peel."""
    db_path = _resolve_path(settings.db_path)
    env_path = PROJECT_ROOT / ".env"
    pyproject_path = PROJECT_ROOT / "pyproject.toml"

    checks = Table(title="Peel doctor")
    checks.add_column("Check", style="bold")
    checks.add_column("Status")
    checks.add_column("Detail")

    checks.add_row(".env", _yes_no(env_path.exists()), str(env_path))
    checks.add_row(
        "Spotify client id",
        _yes_no(bool(settings.spotify_client_id)),
        _masked(settings.spotify_client_id),
    )
    checks.add_row(
        "Spotify client secret",
        _yes_no(bool(settings.spotify_client_secret)),
        _masked(settings.spotify_client_secret),
    )
    checks.add_row(
        "Spotify refresh token",
        _yes_no(bool(settings.spotify_refresh_token)),
        _masked(settings.spotify_refresh_token),
    )
    checks.add_row(
        "Playlist id",
        _yes_no(bool(settings.peel_playlist_id)),
        _masked(settings.peel_playlist_id),
    )
    checks.add_row("DB file", _yes_no(db_path.exists()), str(db_path))

    tables_detail = "-"
    tables_ok = False
    if db_path.exists():
        try:
            with sqlite3.connect(db_path) as conn:
                tables = _list_tables(conn)
                expected = {"tracks", "sources_state", "unmatched", "feedback", "albums"}
                tables_ok = expected.issubset(tables)
                tables_detail = ", ".join(sorted(tables))
        except sqlite3.Error as exc:
            tables_detail = str(exc)

    checks.add_row("Schema", _yes_no(tables_ok), tables_detail)

    script_target = _script_target(pyproject_path)
    checks.add_row(
        "Entrypoint",
        _yes_no(script_target == "peel.cli:app"),
        script_target or "missing",
    )

    checks.add_row(
        "CLI import",
        _yes_no(hasattr(__import__("peel.cli", fromlist=["app"]), "app")),
        "peel.cli:app",
    )

    console.print(checks)


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _scalar(conn: sqlite3.Connection, query: str, params: tuple[object, ...] = ()) -> int:
    row = conn.execute(query, params).fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def _list_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
    ).fetchall()
    return {row[0] for row in rows}


def _fetch_source_states(conn: sqlite3.Connection) -> list[SourceState]:
    rows = conn.execute(
        """
        SELECT source_id, last_run_at, last_status, last_error
        FROM sources_state
        ORDER BY last_run_at DESC, source_id
        """,
    ).fetchall()
    return [
        SourceState(
            source_id=row[0],
            last_run_at=row[1],
            last_status=row[2],
            last_error=row[3],
        )
        for row in rows
    ]


def _fetch_track_summaries(
    conn: sqlite3.Connection,
    limit: int,
) -> list[TrackSummary]:
    rows = conn.execute(
        """
        SELECT
            spotify_uri,
            artist,
            title,
            MAX(added_at_week) AS added_at_week,
            COUNT(DISTINCT source_id) AS source_count,
            MIN(added_at) AS first_added_at,
            MAX(added_at) AS last_added_at
        FROM tracks
        GROUP BY spotify_uri, artist, title
        ORDER BY last_added_at DESC, artist COLLATE NOCASE, title COLLATE NOCASE
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        TrackSummary(
            spotify_uri=row[0],
            artist=row[1],
            title=row[2],
            added_at_week=row[3] or "",
            source_count=int(row[4]),
            first_added_at=row[5] or "",
            last_added_at=row[6] or "",
        )
        for row in rows
    ]


def _feedback_for_track(
    conn: sqlite3.Connection,
    spotify_uri: str,
) -> tuple[int, str, str | None] | None:
    row = conn.execute(
        "SELECT rating, label, comment FROM feedback WHERE spotify_uri = ?",
        (spotify_uri,),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), str(row[1]), row[2]


def _print_track_sources(conn: sqlite3.Connection, row: TrackSummary) -> None:
    table = Table(title=f"{row.artist} — {row.title}", show_header=False)
    table.add_column("Source")
    table.add_column("URL")
    source_rows = conn.execute(
        """
        SELECT source_id, source_url
        FROM tracks
        WHERE spotify_uri = ?
        ORDER BY added_at ASC, source_id
        """,
        (row.spotify_uri,),
    ).fetchall()
    for source_id, source_url in source_rows:
        table.add_row(source_id, source_url or "")
    console.print(table)


def _print_feedback_prompt(
    db: DB,
    row: FeedbackRow,
    index: int,
    total: int,
) -> None:
    spotify_uri, artist, title, week, source_count, first_added_at, last_added_at = row
    panel = Panel(
        f"{index}/{total} {artist} — {title}\n"
        f"Week: {week}\n"
        f"Sources: {source_count}\n"
        f"Added: {first_added_at} → {last_added_at}\n"
        f"URI: {spotify_uri}",
        title="Feedback",
    )
    console.print(panel)

    sources = db.track_sources(spotify_uri)
    if not sources:
        return

    table = Table(show_header=False)
    table.add_column("Source")
    table.add_column("URL")
    for source_id, source_url in sources:
        table.add_row(source_id, source_url or "")
    console.print(table)


def _prompt_rating(default: str) -> str:
    allowed = ", ".join(sorted(FEEDBACK_RATINGS))
    while True:
        value = typer.prompt(f"Rating [{allowed} / q]", default=default).strip().lower()
        if value in {"q", "quit", "exit"}:
            return value
        if value in FEEDBACK_RATINGS:
            return value
        console.print(f"Rating inválida: {value}")


def _prompt_comment(default: str) -> str | None:
    value = typer.prompt("Comment optional", default=default, show_default=False).strip()
    return value or None


def _save_feedback(
    db: DB,
    spotify_uri: str,
    rating: str,
    comment: str | None,
) -> None:
    db.upsert_feedback(spotify_uri, rating, comment)


def _masked(value: str) -> str:
    if not value:
        return "missing"
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}…{value[-3:]}"


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _script_target(pyproject_path: Path) -> str | None:
    if not pyproject_path.exists():
        return None
    with pyproject_path.open("rb") as fh:
        data = tomllib.load(fh)
    scripts = data.get("project", {}).get("scripts", {})
    target = scripts.get("peel")
    return str(target) if target is not None else None
