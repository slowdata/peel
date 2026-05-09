"""CLI do Peel.

Backoffice local para:
- correr o pipeline semanal,
- ver estado da DB,
- listar tracks recentes,
- dar feedback,
- fazer diagnóstico básico do ambiente.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tomllib
import webbrowser
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from peel.config import settings
from peel.db import DB, FEEDBACK_RATINGS
from peel.doctor_sources import inspect_registered_sources
from peel.main import run as run_pipeline
from peel.report import generate_weekly_report
from peel.scoring import SourceScore, build_source_scores

PROJECT_ROOT = Path(__file__).resolve().parents[2]
console = Console(width=120)
app = typer.Typer(add_completion=False, help="Peel — música curada, sincronizada e visível.")
sync_app = typer.Typer(add_completion=False, help="Sincronização local/GitHub.")
doctor_app = typer.Typer(add_completion=False, help="Diagnósticos do Peel.")
app.add_typer(sync_app, name="sync")
app.add_typer(doctor_app, name="doctor")


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


@dataclass(slots=True)
class GitSyncState:
    branch: str
    upstream: str | None
    ahead: int
    behind: int
    dirty: bool
    peel_db_changed: bool
    dirty_paths: list[str]


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
def report(
    week: Annotated[str | None, typer.Option(help="Semana ISO a gerar")] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(help="Directório de saída"),
    ] = None,
    open_report: Annotated[
        bool,
        typer.Option("--open", help="Abre o relatório no browser"),
    ] = False,
) -> None:
    """Gera o relatório semanal em Markdown."""
    db = DB(str(_resolve_path(settings.db_path)))
    try:
        db.init_schema()
        target_dir = output_dir or PROJECT_ROOT / "data" / "reports"
        try:
            path = generate_weekly_report(db, week=week, output_dir=target_dir)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--week") from exc
        console.print(f"Report written: {path}")
        if open_report:
            webbrowser.open(path.resolve().as_uri())
    finally:
        db.close()


@app.command()
def sources(
    weeks: int = typer.Option(4, "--weeks", min=1, help="Janela em semanas"),
    min_tracks: int = typer.Option(
        0,
        "--min-tracks",
        min=0,
        help="Filtra sources com menos tracks matched",
    ),
    json_output: bool = typer.Option(False, "--json", help="Saída JSON"),
) -> None:
    """Mostra scoring das sources com base nos dados existentes."""
    if json_output:
        with redirect_stdout(sys.stderr):
            rows = _source_score_rows(weeks, min_tracks)
        typer.echo(
            json.dumps(
                [_source_score_to_dict(row) for row in rows],
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    rows = _source_score_rows(weeks, min_tracks)
    if not rows:
        console.print("Sem dados para scoring.")
        return

    table = Table(title=f"Source scores (last {weeks} weeks)")
    table.add_column("Source", style="bold")
    table.add_column("Found", justify="right")
    table.add_column("Matched", justify="right")
    table.add_column("New", justify="right")
    table.add_column("Dup", justify="right")
    table.add_column("Consensus", justify="right")
    table.add_column("Unmatched", justify="right")
    table.add_column("Liked", justify="right")
    table.add_column("Skipped", justify="right")
    table.add_column("Avg rating", justify="right")
    table.add_column("Score", justify="right")

    for row in rows:
        table.add_row(
            row.source_id,
            str(row.tracks_found),
            str(row.tracks_matched),
            str(row.new_unique_tracks),
            str(row.duplicate_mentions),
            str(row.consensus_hits),
            str(row.unmatched_count),
            str(row.liked_count),
            str(row.skipped_count),
            row.avg_rating_display,
            f"{row.score:.1f}",
        )

    console.print(table)


@sync_app.command("status")
def sync_status() -> None:
    """Mostra estado do git local e do upstream."""
    state = _git_sync_state()

    table = Table(title="Git sync")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Branch", state.branch)
    table.add_row("Upstream", state.upstream or "-")
    table.add_row("Dirty", _yes_no(state.dirty))
    table.add_row("Ahead", str(state.ahead))
    table.add_row("Behind", str(state.behind))
    table.add_row("data/peel.db changed", _yes_no(state.peel_db_changed))
    table.add_row(
        "Dirty paths",
        ", ".join(state.dirty_paths) if state.dirty_paths else "-",
    )
    console.print(Panel(table, title="Sync status"))


@sync_app.command("pull")
def sync_pull() -> None:
    """Faz pull seguro do upstream."""
    state = _git_sync_state()
    if state.dirty:
        console.print("Working tree dirty; aborting pull.")
        raise typer.Exit(code=1)

    result = _run_git(["pull", "--ff-only"])
    if result.returncode != 0:
        _print_git_error(result)
        raise typer.Exit(code=result.returncode or 1)

    console.print("Pull completed.")


@sync_app.command("push")
def sync_push() -> None:
    """Faz commit + push do estado local."""
    staged_paths = _git_staged_paths()
    forbidden_paths = [path for path in staged_paths if not _sync_path_allowed(path)]
    if forbidden_paths:
        console.print("Staged files outside Peel sync scope:")
        for path in forbidden_paths:
            console.print(f"- {path}")
        raise typer.Exit(code=1)

    state = _git_sync_state()

    paths = [_project_path("data/peel.db")]
    reports_dir = _project_path("data/reports")
    if reports_dir.exists():
        paths.append(reports_dir)

    add_result = _run_git(["add", *[str(path.relative_to(PROJECT_ROOT)) for path in paths]])
    if add_result.returncode != 0:
        _print_git_error(add_result)
        raise typer.Exit(code=add_result.returncode or 1)

    diff_result = _run_git(["diff", "--cached", "--quiet"])
    has_staged_changes = diff_result.returncode != 0
    if not has_staged_changes and state.ahead == 0:
        console.print("Nothing to push.")
        return

    if has_staged_changes:
        commit_result = _run_git(["commit", "-m", "chore: update peel local feedback/state"])
        if commit_result.returncode != 0:
            _print_git_error(commit_result)
            raise typer.Exit(code=commit_result.returncode or 1)

    push_result = _run_git(["push"])
    if push_result.returncode != 0:
        _print_git_error(push_result)
        raise typer.Exit(code=push_result.returncode or 1)

    console.print("Push completed.")


@doctor_app.callback(invoke_without_command=True)
def doctor(ctx: typer.Context) -> None:
    """Valida o ambiente local do Peel."""
    if ctx.invoked_subcommand is not None:
        return
    _print_doctor_overview()


@doctor_app.command("sources")
def doctor_sources(json_output: bool = typer.Option(False, "--json", help="Saída JSON")) -> None:
    """Valida a registry de sources disponível no repositório."""
    results = inspect_registered_sources()
    if json_output:
        typer.echo(
            json.dumps([result.to_dict() for result in results], ensure_ascii=False, indent=2)
        )
        return

    if not results:
        console.print("Sem sources para validar.")
        return

    table = Table(title="Doctor sources")
    table.add_column("Source", style="bold")
    table.add_column("Type")
    table.add_column("HTTP", justify="right")
    table.add_column("Entries", justify="right")
    table.add_column("OK")
    table.add_column("Note")

    for result in results:
        table.add_row(
            result.name,
            result.type,
            str(result.http_status),
            str(result.entries),
            _yes_no(result.ok),
            result.note,
        )

    console.print(table)


def _source_score_rows(weeks: int, min_tracks: int) -> list[SourceScore]:
    db = DB(str(_resolve_path(settings.db_path)))
    try:
        db.init_schema()
        return [
            row for row in build_source_scores(db, weeks=weeks) if row.tracks_matched >= min_tracks
        ]
    finally:
        db.close()


def _source_score_to_dict(row: SourceScore) -> dict[str, object]:
    return {
        "source_id": row.source_id,
        "tracks_found": row.tracks_found,
        "tracks_matched": row.tracks_matched,
        "new_unique_tracks": row.new_unique_tracks,
        "duplicate_mentions": row.duplicate_mentions,
        "consensus_hits": row.consensus_hits,
        "unmatched_count": row.unmatched_count,
        "liked_count": row.liked_count,
        "skipped_count": row.skipped_count,
        "avg_rating": row.avg_rating,
        "score": row.score,
    }


def _print_doctor_overview() -> None:
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
                expected = {
                    "tracks",
                    "sources_state",
                    "unmatched",
                    "feedback",
                    "albums",
                    "source_runs",
                }
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


def _project_path(value: str) -> Path:
    return PROJECT_ROOT / value


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def _print_git_error(result: subprocess.CompletedProcess[str]) -> None:
    message = (result.stderr or result.stdout or "git command failed").strip()
    console.print(message)


def _git_staged_paths() -> list[str]:
    result = _run_git(["diff", "--cached", "--name-only"])
    if result.returncode != 0:
        _print_git_error(result)
        raise typer.Exit(code=result.returncode or 1)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _sync_path_allowed(path: str) -> bool:
    return path == "data/peel.db" or path == "data/reports" or path.startswith("data/reports/")


def _git_sync_state() -> GitSyncState:
    result = _run_git(["status", "--porcelain=v1", "-b"])
    if result.returncode != 0:
        _print_git_error(result)
        raise typer.Exit(code=result.returncode or 1)

    lines = result.stdout.splitlines()
    header = lines[0] if lines else ""
    branch, upstream, ahead, behind = _parse_git_header(header)
    dirty_paths = [line[3:].strip() for line in lines[1:] if line.strip()]
    dirty = bool(dirty_paths)
    peel_db_changed = any(path.endswith("data/peel.db") for path in dirty_paths)
    return GitSyncState(
        branch=branch,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        dirty=dirty,
        peel_db_changed=peel_db_changed,
        dirty_paths=dirty_paths,
    )


def _parse_git_header(header: str) -> tuple[str, str | None, int, int]:
    if not header.startswith("## "):
        return "unknown", None, 0, 0

    payload = header[3:]
    branch = payload
    upstream: str | None = None
    ahead = 0
    behind = 0

    if "..." in payload:
        branch_part, remainder = payload.split("...", 1)
        branch = branch_part.strip() or "unknown"
        if " [" in remainder:
            upstream_part, details_part = remainder.split(" [", 1)
            upstream = upstream_part.strip() or None
            details = details_part.rstrip("]")
            ahead, behind = _parse_ahead_behind(details)
        else:
            upstream = remainder.strip() or None
    else:
        if " [" in payload:
            branch_part, details_part = payload.split(" [", 1)
            branch = branch_part.strip() or "unknown"
            details = details_part.rstrip("]")
            ahead, behind = _parse_ahead_behind(details)
        else:
            branch = payload.strip() or "unknown"

    return branch, upstream, ahead, behind


def _parse_ahead_behind(details: str) -> tuple[int, int]:
    ahead = 0
    behind = 0
    for part in details.split(","):
        item = part.strip()
        if item.startswith("ahead "):
            ahead = int(item.split()[1])
        elif item.startswith("behind "):
            behind = int(item.split()[1])
    return ahead, behind
