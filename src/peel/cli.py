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
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import webbrowser
from collections.abc import Callable, Mapping
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from peel.affinity import build_affinity_profile
from peel.album_discovery import AlbumDiscoveryError, discover_album_mentions
from peel.albums import CANONICAL_ALBUM_QUEUE_SINCE, select_album_queue
from peel.config import settings
from peel.db import ALBUM_FEEDBACK_RATINGS, DB, FEEDBACK_RATINGS, iso_week
from peel.doctor_sources import inspect_registered_sources
from peel.listening import archive_report_files
from peel.main import (
    MAX_ALBUM_QUEUE_ITEMS,
    MAX_ALBUM_RESOLUTION_CANDIDATES,
    _album_queue_snapshot_items,
    build_triage_items,
    configure_logging,
)
from peel.main import run as run_pipeline
from peel.matcher import normalize
from peel.models import AlbumQueueItem, ReviewQueueItem
from peel.musicbrainz import fetch_musicbrainz_artist_genres
from peel.playlists import canonical_playlist_id
from peel.publication import (
    Candidate,
    PublicationPlan,
    album_candidates,
    build_plan,
    default_picks,
    parse_positions,
    prepare_publication,
    track_candidates,
)
from peel.report import generate_weekly_html_report, generate_weekly_report
from peel.scoring import SourceScore, build_source_overview, build_source_scores
from peel.site_export import CANONICAL_PUBLIC_SELECTION_SINCE, export_site, make_album_resolver
from peel.sources.registry import ACTIVE_SOURCES, source_label
from peel.spotify_client import SpotifyClient, SpotifyReauthRequired
from peel.state_sync import (
    STATE_CLONE_URL,
    STATE_REPOSITORY,
    LocalStateConflict,
    StateSyncError,
    git_blob_sha,
    latest_state_week,
    mark_local_state_synced,
    merge_remote_state_for_push,
    state_has_local_changes,
    sync_remote_state,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Ordem dos atalhos numéricos: do mais positivo ao mais negativo.
RATING_ORDER = ("love", "like", "meh", "skip", "ban", "unavailable")
MAX_TRIAGE_SESSION = 100
_OFFLINE_MODE = False


def _configure_stdin_decode_errors() -> None:
    """Evita crashes em prompts quando o terminal envia bytes UTF-8 inválidos."""
    reconfigure = getattr(sys.stdin, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="replace")


_configure_stdin_decode_errors()

console = Console(width=160)
app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    help="Peel — música curada, sincronizada e visível. Sem comando, mostra o estado.",
)
sync_app = typer.Typer(add_completion=False, help="Sincronização local/GitHub.")
doctor_app = typer.Typer(add_completion=False, help="Diagnósticos do Peel.")
triage_app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    help="Fila de triagem confirmada no Spotify.",
)
site_app = typer.Typer(add_completion=False, help="Exportação para o site peel-sept.")
affinity_app = typer.Typer(add_completion=False, help="Perfil local de afinidade.")
albums_app = typer.Typer(
    add_completion=False, invoke_without_command=True, help="Fila semanal canónica de álbuns."
)
PANEL_WEEK = "Semana"
PANEL_BROWSE = "Consultar"
PANEL_MAINTENANCE = "Manutenção"
app.add_typer(albums_app, name="albums", rich_help_panel=PANEL_BROWSE)
app.add_typer(sync_app, name="sync", rich_help_panel=PANEL_MAINTENANCE)
app.add_typer(doctor_app, name="doctor", rich_help_panel=PANEL_MAINTENANCE)
# Internos: continuam a funcionar (GitHub, publicar, diagnóstico), mas fora da ajuda.
app.add_typer(triage_app, name="triage", hidden=True)
app.add_typer(site_app, name="site", hidden=True)
app.add_typer(affinity_app, name="affinity", hidden=True)


@app.callback()
def cli_callback(
    ctx: typer.Context,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Mostra logs estruturados locais para diagnóstico"),
    ] = False,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Não consulta o estado canónico remoto"),
    ] = False,
) -> None:
    """Configura logging e o modo de estado para a CLI local."""
    global _OFFLINE_MODE  # noqa: PLW0603 - Typer callback owns this process-wide option
    _OFFLINE_MODE = offline or _env_truthy("PEEL_OFFLINE")
    configure_logging(verbose=verbose, pipeline=ctx.invoked_subcommand == "run")
    if ctx.invoked_subcommand is None:
        estado()


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


@app.command("run", hidden=True)
def run_command(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Simula a run sem escrever DB, playlists ou enviar Telegram",
        ),
    ] = False,
) -> None:
    """Executa a pipeline semanal do Peel (use --dry-run para pré-visualizar sem impacto)."""
    try:
        run_pipeline(dry_run=dry_run)
    except SpotifyReauthRequired as exc:
        _abort_spotify_reauth(exc)


@app.command("finalize", hidden=True)
def finalize(
    week: Annotated[
        str | None, typer.Option("--week", help="Semana ISO a publicar (default: a atual)")
    ] = None,
    site_dir: Annotated[
        Path, typer.Option("--site-dir", help="Diretório do site peel-sept")
    ] = Path("../peel-sept"),
    export: Annotated[
        bool, typer.Option("--export/--no-export", help="Exportar o site após finalizar")
    ] = True,
    selection: Annotated[
        Path | None, typer.Option("--selection", help="JSON da selecção aprovada")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validar sem quaisquer escritas externas")
    ] = False,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Substituir explicitamente uma edição finalizada")
    ] = False,
) -> None:
    """Finaliza apenas a semana escolhida, com selecção explícita desde W38."""
    plan = None
    if selection is not None:
        try:
            plan = PublicationPlan.model_validate_json(selection.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise typer.BadParameter(str(exc), param_hint="--selection") from exc
        if week and _normalize_week_option(week) != plan.week:
            raise typer.BadParameter("Semana do plano difere de --week")
        week = plan.week
    target_week = _normalize_week_option(week) if week else iso_week(datetime.now(UTC))
    snapshot_playlist_id = canonical_playlist_id(settings.peel_playlist_id)
    review_id = settings.peel_review_playlist_id or settings.peel_playlist_id
    if plan and (
        canonical_playlist_id(plan.playlist_id) != snapshot_playlist_id
        or canonical_playlist_id(plan.review_playlist_id) != canonical_playlist_id(review_id)
        or canonical_playlist_id(review_id) == snapshot_playlist_id
    ):
        raise typer.BadParameter("Playlists do plano não correspondem à configuração")
    if not dry_run:
        _auto_sync_state("finalize")
    temporary = tempfile.TemporaryDirectory(prefix="peel-finalize-") if dry_run else None
    db_path = _resolve_path(settings.db_path)
    if temporary is not None:
        copied = Path(temporary.name) / "peel.db"
        with (
            sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as source,
            sqlite3.connect(copied) as dest,
        ):
            source.backup(dest)
        db_path = copied
    db = DB(str(db_path))
    try:
        db.init_schema()
        if week is None:
            target_week = (
                db.active_review_week(settings.peel_review_playlist_id or settings.peel_playlist_id)
                or target_week
            )
        if db.is_archived_week(target_week):
            raise typer.BadParameter(f"Semana {target_week} arquivada; finalize cancelado.")
        saved = db.finalized_week_selection(target_week, snapshot_playlist_id)
        selected = saved
        if plan is not None:
            if saved and saved["plan"] != plan.model_dump(mode="json") and not refresh:
                raise typer.BadParameter(
                    "Edição já finalizada; usa --refresh para mudar a selecção"
                )
            if not saved or refresh:
                try:
                    selected = prepare_publication(db, plan)
                except ValueError as exc:
                    raise typer.BadParameter(str(exc), param_hint="--selection") from exc
        if target_week >= CANONICAL_PUBLIC_SELECTION_SINCE and selected is None:
            raise typer.BadParameter("Indica --selection com as faixas e álbuns aprovados")
        previous = db.finalized_week_uris(target_week, snapshot_playlist_id)
        keepers = (
            selected["track_uris"]
            if selected is not None
            else (
                previous
                if previous is not None and not refresh
                else db.week_keeper_uris(target_week)
            )
        )
        if dry_run:
            console.print_json(data=selected or {"week": target_week, "track_uris": keepers})
            console.print("Dry-run: sem alterações à DB, Spotify, Telegram ou site.")
            return
        try:
            sp = SpotifyClient()
        except SpotifyReauthRequired as exc:
            _abort_spotify_reauth(exc)
        sp.replace_playlist_items(settings.peel_playlist_id, keepers)
        db.replace_finalized_week_tracks(
            target_week, snapshot_playlist_id, keepers, selection=selected
        )
        console.print(
            f"Finalized {target_week}: {len(keepers)} keepers → {settings.peel_playlist_id}"
        )
        if export:
            try:
                export_site(
                    db,
                    _resolve_path(str(site_dir)),
                    weeks=1,
                    playlist_id=snapshot_playlist_id,
                    current_week=target_week,
                    album_resolver=make_album_resolver(sp),
                )
            except Exception:
                console.print(
                    "Export falhou depois da confirmação Spotify; o snapshot ficou guardado. "
                    f"Corre uv run peel site export --week {target_week} --weeks 1 para repetir."
                )
                raise
            console.print("Site exported.")
        console.print("Corre uv run peel sync push.")
    finally:
        db.close()
        if temporary is not None:
            temporary.cleanup()


SITE_URL = "https://peel.sept.pt/pt"
LISBON = ZoneInfo("Europe/Lisbon")
# Manter alinhado com o cron de .github/workflows/weekly.yml ("17 17 * * 5").
WEEKLY_RUN_WEEKDAY, WEEKLY_RUN_HOUR, WEEKLY_RUN_MINUTE = 4, 17, 17
_WEEKDAYS_PT = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")
_MONTHS_PT = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")


def next_weekly_run(now: datetime) -> datetime:
    """Próximo agendamento da execução semanal (UTC), estritamente depois de ``now``."""
    candidate = now.astimezone(UTC).replace(
        hour=WEEKLY_RUN_HOUR, minute=WEEKLY_RUN_MINUTE, second=0, microsecond=0
    )
    candidate += timedelta(days=(WEEKLY_RUN_WEEKDAY - candidate.weekday()) % 7)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def _when_pt(moment: datetime) -> str:
    local = moment.astimezone(LISBON)
    return (
        f"{_WEEKDAYS_PT[local.weekday()]} {local.day} {_MONTHS_PT[local.month - 1]}, {local:%H:%M}"
    )


def _spotify_web_url(uri_or_url: str) -> str:
    """``spotify:track:ID`` → ``https://open.spotify.com/track/ID``."""
    if uri_or_url.startswith("spotify:"):
        _, kind, value = uri_or_url.split(":", 2)
        return f"https://open.spotify.com/{kind}/{value}"
    return uri_or_url


def _site_has_week(week: str, site_dir: Path | None = None) -> bool:
    root = _resolve_path(str(site_dir or Path("../peel-sept")))
    return (root / "src" / "data" / "weeks" / f"{week}.json").exists()


@app.command(rich_help_panel=PANEL_WEEK)
def estado() -> None:
    """Resumo: semana activa, o que falta avaliar, publicação e próxima execução."""
    _auto_sync_state("estado")
    review_id = settings.peel_review_playlist_id or settings.peel_playlist_id
    db = DB(str(_resolve_path(settings.db_path)))
    try:
        db.init_schema()
        week = db.active_review_week(review_id)
        if week is None:
            console.print("Sem fila activa confirmada. Aguarda a próxima execução.")
            return
        queue = db.review_queue_snapshot(review_id, week) or []
        albums = db.album_queue(week) or []
        unrated_tracks = sum(db.feedback_for_track_identity(i.spotify_uri) is None for i in queue)
        unrated_albums = sum(
            db.album_feedback_for_identity(i.artist, i.album) is None for i in albums
        )
        selection = db.finalized_week_selection(week, settings.peel_playlist_id)
        confirmed = db.conn.execute(
            "SELECT confirmed_at FROM review_queue_snapshots WHERE week=? AND playlist_id=?",
            (week, review_id),
        ).fetchone()
    finally:
        db.close()

    now = datetime.now(UTC)
    console.print(f"[bold]Peel — {week}[/bold]")
    if confirmed:
        console.print(f"  Fila entregue: {_when_pt(datetime.fromisoformat(confirmed[0]))}")
    console.print(f"  Faixas: {len(queue)} · por avaliar {unrated_tracks}")
    console.print(f"  Álbuns: {len(albums)} · por avaliar {unrated_albums}")
    if selection:
        console.print(
            f"  Publicada: {len(selection['tracks'])} faixas · {len(selection['albums'])} álbuns"
            f" → {SITE_URL}/{week}"
        )
    else:
        console.print("  Publicada: ainda não")
    run_at = next_weekly_run(now)
    today_run = run_at - timedelta(days=7)
    if today_run.date() == now.date() and week < iso_week(now):
        console.print(
            f"  Execução de hoje ({_when_pt(today_run)}): ainda não chegou;"
            " o GitHub pode atrasar até ~4h"
        )
    else:
        console.print(f"  Próxima execução: {_when_pt(run_at)} (o GitHub pode atrasar)")
    if unrated_tracks or unrated_albums:
        step = "uv run peel ouvir"
    elif not selection:
        step = "uv run peel publicar"
    else:
        step = "nada até à próxima execução 🎧"
    console.print(f"[bold]Próximo passo:[/bold] {step}")


@dataclass(frozen=True, slots=True)
class WeekTrack:
    position: int
    artist: str
    title: str
    spotify_uri: str
    rating: str | None
    published: bool
    source_id: str | None


def _week_tracks(db: DB, week: str) -> list[WeekTrack] | None:
    """Faixas ouvidas numa semana (fila confirmada) ou, sem fila, as publicadas."""
    review_id = settings.peel_review_playlist_id or settings.peel_playlist_id
    published = set(db.finalized_week_uris(week, settings.peel_playlist_id) or [])
    queue = db.review_queue_snapshot(review_id, week)
    if queue:
        return [
            WeekTrack(
                position,
                item.artist,
                item.title,
                item.spotify_uri,
                (db.feedback_for_track_identity(item.spotify_uri) or (None, None))[1],
                item.spotify_uri in published,
                item.source_id,
            )
            for position, item in enumerate(queue, 1)
        ]
    uris = db.finalized_week_uris(week, settings.peel_playlist_id)
    if not uris:
        return None
    rows = []
    for position, uri in enumerate(uris, 1):
        row = db.conn.execute(
            "SELECT artist, title, source_id FROM tracks WHERE spotify_uri=? LIMIT 1", (uri,)
        ).fetchone()
        artist, title, source_id = row if row else ("?", uri, None)
        rating = (db.feedback_for_track_identity(uri) or (None, None))[1]
        rows.append(WeekTrack(position, artist, title, uri, rating, True, source_id))
    return rows


def _listed_weeks(db: DB) -> list[str]:
    review_id = settings.peel_review_playlist_id or settings.peel_playlist_id
    rows = db.conn.execute(
        "SELECT week FROM review_queue_snapshots WHERE playlist_id=? "
        "UNION SELECT week FROM finalized_week_tracks WHERE playlist_id=? ORDER BY week DESC",
        (canonical_playlist_id(review_id), canonical_playlist_id(settings.peel_playlist_id)),
    ).fetchall()
    return [str(row[0]) for row in rows]


@app.command(rich_help_panel=PANEL_BROWSE)
def musicas(
    semana: Annotated[
        str | None, typer.Option("--semana", "--week", help="Semana ISO (default: a activa)")
    ] = None,
    semanas: Annotated[
        bool, typer.Option("--semanas", help="Lista as semanas disponíveis")
    ] = False,
    abrir: Annotated[
        int | None, typer.Option("--abrir", "-o", min=1, help="Abre a faixa nº N no Spotify")
    ] = None,
    playlist: Annotated[
        bool, typer.Option("--playlist", help="Abre a playlist desta semana no Spotify")
    ] = False,
    site: Annotated[bool, typer.Option("--site", help="Abre esta semana no site")] = False,
) -> None:
    """Faixas por semana: avaliação, publicação; abre faixa, playlist ou site."""
    _auto_sync_state("musicas")
    review_id = settings.peel_review_playlist_id or settings.peel_playlist_id
    db = DB(str(_resolve_path(settings.db_path)))
    try:
        db.init_schema()
        if semanas:
            table = Table(title="Peel — semanas")
            for column in ("Semana", "Faixas", "Avaliadas", "Publicadas"):
                table.add_column(column, justify="right" if column != "Semana" else "left")
            for week in _listed_weeks(db):
                rows = _week_tracks(db, week) or []
                rated = sum(row.rating is not None for row in rows)
                public = sum(row.published for row in rows)
                table.add_row(week, str(len(rows)), str(rated), str(public) if public else "—")
            console.print(table)
            return
        week = _normalize_week_option(semana) if semana else db.active_review_week(review_id)
        if week is None:
            console.print("Sem fila activa confirmada.")
            raise typer.Exit(code=1)
        rows = _week_tracks(db, week)
        active_week = db.active_review_week(review_id)
        latest_published = db.conn.execute(
            "SELECT MAX(week) FROM finalized_week_tracks WHERE playlist_id=?",
            (canonical_playlist_id(settings.peel_playlist_id),),
        ).fetchone()[0]
    finally:
        db.close()
    if not rows:
        console.print(f"Sem lista de faixas para {week}. Vê as semanas com: peel musicas --semanas")
        raise typer.Exit(code=1)

    table = Table(title=f"Peel — {week} ({len(rows)} faixas)")
    table.add_column("#", justify="right")
    table.add_column("Avaliação")
    table.add_column("Site")
    table.add_column("Artista", style="bold")
    table.add_column("Título")
    table.add_column("Fonte")
    for row in rows:
        table.add_row(
            str(row.position),
            row.rating or "por avaliar",
            "✓" if row.published else "",
            row.artist,
            row.title,
            source_label(row.source_id) if row.source_id else "",
        )
    console.print(table)

    if abrir is not None:
        match = next((row for row in rows if row.position == abrir), None)
        if match is None:
            raise typer.BadParameter(f"Não há faixa nº {abrir} em {week}", param_hint="--abrir")
        _open_listen_url(_spotify_web_url(match.spotify_uri))
    if playlist:
        if week == active_week:
            playlist_id = review_id
        elif week == latest_published:
            playlist_id = settings.peel_playlist_id
        else:
            console.print(
                "A playlist do Spotify só tem a semana activa (triagem) e a última publicada."
                " Para semanas antigas, usa --site."
            )
            raise typer.Exit(code=1)
        _open_listen_url(f"https://open.spotify.com/playlist/{canonical_playlist_id(playlist_id)}")
    if site:
        if not _site_has_week(week):
            console.print(f"{week} ainda não está no site. Publica com: uv run peel publicar")
            raise typer.Exit(code=1)
        webbrowser.open(f"{SITE_URL}/{week}")


@app.command(rich_help_panel=PANEL_WEEK)
def ouvir(
    push: Annotated[
        bool, typer.Option("--push/--no-push", help="Enviar as avaliações no fim")
    ] = True,
    rever: Annotated[
        bool, typer.Option("--rever", help="Rever notas já dadas (ex.: f5, a11)")
    ] = False,
    semana: Annotated[
        str | None, typer.Option("--semana", help="Semana a rever (default: a activa)")
    ] = None,
) -> None:
    """Avalia faixas e depois álbuns da fila activa, e envia tudo no fim."""
    if semana and not rever:
        raise typer.BadParameter("--semana só se usa com --rever", param_hint="--semana")
    _auto_sync_state("ouvir")
    db = DB(str(_resolve_path(settings.db_path)))
    try:
        db.init_schema()
        if rever:
            review_id = settings.peel_review_playlist_id or settings.peel_playlist_id
            week = _normalize_week_option(semana) if semana else db.active_review_week(review_id)
            if week is None:
                console.print("Sem fila activa confirmada.")
                saved = 0
            else:
                saved = _review_ratings(db, week)
        else:
            saved = _rate_unrated(db)
    finally:
        db.close()
    if not saved:
        console.print("Nada novo para enviar.")
    elif not push:
        console.print(f"{saved} avaliações guardadas. Corre uv run peel sync push.")
    else:
        console.print(f"A enviar {saved} avaliações…")
        console.print("Enviado." if _push_state() else "Nada para enviar.")


def _rate_unrated(db: DB) -> int:
    """Sessão normal: faixas por avaliar, depois os álbuns."""
    console.print("[bold]Faixas[/bold] — Enter = like; 1 love · 2 like · 3 meh · 4 skip · 5 ban")
    saved = _run_triage_feedback_session(db, limit=MAX_TRIAGE_SESSION, push_hint=False)
    items = db.latest_album_queue(settings.peel_review_playlist_id or settings.peel_playlist_id)
    if items and typer.confirm("Avaliar os álbuns agora?", default=True):
        saved += _run_album_feedback_session(db, items, items[0].week, push_hint=False)
    return saved


def _review_ratings(db: DB, week: str) -> int:
    """Mostra as notas da semana e deixa mudar as escolhidas (f5 = faixa, a11 = álbum)."""
    review_id = settings.peel_review_playlist_id or settings.peel_playlist_id
    tracks = db.review_queue_snapshot(review_id, week) or []
    albums = db.album_queue(week) or []
    if not tracks and not albums:
        console.print(f"Sem fila confirmada para {week}.")
        return 0
    table = Table(title=f"Notas — {week}")
    for column in ("", "Nota", "Artista", "Título"):
        table.add_column(column, style="bold" if column == "Artista" else None)
    for position, item in enumerate(tracks, 1):
        rating = db.feedback_for_track_identity(item.spotify_uri)
        table.add_row(f"f{position}", rating[1] if rating else "—", item.artist, item.title)
    for item in albums:
        rating = db.album_feedback_for_identity(item.artist, item.album)
        table.add_row(f"a{item.position}", rating[1] if rating else "—", item.artist, item.album)
    console.print(table)
    albums_by_position = {item.position: item for item in albums}
    changed = 0
    while True:
        answer = typer.prompt(
            "Mudar qual? (ex.: f5 ou a11; Enter termina)", default="", show_default=False
        )
        code = answer.strip().lower()
        if not code:
            return changed
        kind, number = code[:1], code[1:]
        if kind not in {"f", "a"} or not number.isdigit():
            console.print("Usa f e o número da faixa, ou a e o número do álbum.")
            continue
        position = int(number)
        if kind == "f":
            if not 1 <= position <= len(tracks):
                console.print(f"Não há faixa f{position}.")
                continue
            track = tracks[position - 1]
            current = db.feedback_for_track_identity(track.spotify_uri)
            console.print(f"{track.artist} — {track.title}")
            label = _prompt_rating(
                default=current[1] if current else "like",
                open_item=lambda uri=track.spotify_uri: _open_listen_url(_spotify_web_url(uri)),
            )
            if label in {"q", "quit", "exit"}:
                return changed
            comment = _prompt_comment(default=(current[2] if current else None) or "")
            db.upsert_feedback(track.spotify_uri, label, comment)
        else:
            album = albums_by_position.get(position)
            if album is None:
                console.print(f"Não há álbum a{position}.")
                continue
            current = db.album_feedback_for_identity(album.artist, album.album)
            console.print(f"{album.artist} — {album.album}")
            label = _prompt_rating(
                default=current[1] if current else "like",
                ratings=ALBUM_FEEDBACK_RATINGS,
                open_item=(
                    (lambda url=album.listen_url: _open_listen_url(url))
                    if album.listen_url
                    else None
                ),
            )
            if label in {"q", "quit", "exit"}:
                return changed
            comment = _prompt_comment(default=(current[2] if current else None) or "")
            db.upsert_album_feedback(album.artist, album.album, label, comment)
        changed += 1
        console.print(f"Guardado: {code} → {label}")


@app.command(rich_help_panel=PANEL_WEEK)
def publicar(
    site_dir: Annotated[
        Path, typer.Option("--site-dir", help="Diretório do site peel-sept")
    ] = Path("../peel-sept"),
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Só mostrar a proposta, sem publicar")
    ] = False,
    refazer: Annotated[
        bool, typer.Option("--refazer", help="Reabrir uma edição já publicada")
    ] = False,
    semana: Annotated[
        str | None, typer.Option("--semana", help="Semana a refazer (default: a activa)")
    ] = None,
) -> None:
    """Escolhe e publica a edição da fila activa: Spotify, site e estado."""
    if semana and not refazer:
        raise typer.BadParameter("--semana só se usa com --refazer", param_hint="--semana")
    _auto_sync_state("publicar")
    review_id = settings.peel_review_playlist_id or settings.peel_playlist_id
    db = DB(str(_resolve_path(settings.db_path)))
    try:
        db.init_schema()
        week = _normalize_week_option(semana) if semana else db.active_review_week(review_id)
        if week is None:
            console.print("Sem fila activa confirmada.")
            raise typer.Exit(code=1)
        published = db.finalized_week_selection(week, settings.peel_playlist_id)
        if published is not None and not refazer:
            console.print(f"{week} já está publicada. Para mudar: uv run peel publicar --refazer")
            return
        if published is None and refazer:
            console.print(f"{week} ainda não foi publicada. Usa: uv run peel publicar")
            raise typer.Exit(code=1)
        track_pool = track_candidates(db, review_id, week)
        album_pool = album_candidates(db, week)
        current_tracks = current_albums = None
        if published is not None:
            current_tracks = _published_picks(published["plan"]["tracks"], track_pool, "faixa")
            current_albums = _published_picks(published["plan"]["albums"], album_pool, "álbum")
        tracks = _choose_picks("Faixas", track_pool, current=current_tracks)
        if not tracks:
            console.print("Sem faixas love/like nesta fila. Avalia primeiro com: uv run peel ouvir")
            raise typer.Exit(code=1)
        albums = _choose_picks("Álbuns", album_pool, current=current_albums)
        plan = build_plan(
            db,
            week=week,
            playlist_id=settings.peel_playlist_id,
            review_playlist_id=review_id,
            tracks=tracks,
            albums=albums,
            note=(
                "Selecção refeita com peel publicar --refazer."
                if refazer
                else "Selecção escolhida com peel publicar."
            ),
        )
        if published is not None:
            # Correcções de nome feitas na selecção original continuam válidas.
            overrides = published["plan"].get("artist_overrides", {})
            plan = plan.model_copy(
                update={
                    "artist_overrides": {
                        uri: name for uri, name in overrides.items() if uri in plan.tracks
                    }
                }
            )
        prepare_publication(db, plan)  # validação completa antes de qualquer escrita
    finally:
        db.close()

    console.print(f"\n[bold]{week}[/bold]: {len(tracks)} faixas · {len(albums)} álbuns")
    if dry_run:
        console.print("Dry-run: nada publicado.")
        return
    if not typer.confirm("Publicar no Spotify e no site?", default=False):
        console.print("Cancelado; nada publicado.")
        return
    site_root = _resolve_path(str(site_dir))
    _prepare_site_checkout(site_root)
    with tempfile.TemporaryDirectory(prefix="peel-publicar-") as tmp:
        plan_path = Path(tmp) / f"{week}.json"
        plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        finalize(week=week, site_dir=site_dir, export=True, selection=plan_path, refresh=refazer)
    _publish_site(site_root, week)
    _push_state()
    console.print(f"{week} publicada: Spotify, site e estado.")


def _published_picks(keys: list, candidates: list[Candidate], noun: str) -> list[Candidate]:
    """Selecção publicada, na ordem pública; avisa do que deixou de ser positivo."""
    by_key = {candidate.key: candidate for candidate in candidates}
    picks = []
    for raw in keys:
        key = tuple(raw) if isinstance(raw, list) else raw
        if key in by_key:
            picks.append(by_key[key])
        else:
            label = " — ".join(key) if isinstance(key, tuple) else key
            console.print(f"Aviso: {noun} publicada já não tem love/like e sai: {label}")
    return picks


def _choose_picks(
    title: str, candidates: list[Candidate], current: list[Candidate] | None = None
) -> list[Candidate]:
    """Mostra os candidatos positivos e devolve a escolha, pela ordem pública."""
    if not candidates:
        console.print(f"\n[bold]{title}[/bold]: sem candidatos love/like.")
        return []
    proposal = current if current is not None else default_picks(candidates)
    chosen = {candidate.position for candidate in proposal}
    origin = "publicada" if current is not None else "love antes de like, ordem da fila"
    console.print(f"\n[bold]{title}[/bold] — proposta ✓ ({origin})")
    for candidate in candidates:
        mark = "✓" if candidate.position in chosen else " "
        console.print(
            f" {mark} #{candidate.position:<3} {candidate.rating:<4} "
            f"{candidate.artist} — {candidate.title}"
        )
    while True:
        answer = typer.prompt(
            "Enter aceita ✓; ou escreve as posições pela ordem pública (ex.: 5 13 28)",
            default="",
            show_default=False,
        )
        if not answer.strip():
            return proposal
        try:
            return parse_positions(answer, candidates)
        except ValueError as exc:
            console.print(str(exc))


def _git(site_root: Path, *args: str, capture: bool = False) -> str:
    result = subprocess.run(  # noqa: S603 - fixed git binary and arguments
        ["git", "-C", str(site_root), *args],
        check=True,
        text=True,
        capture_output=capture,
    )
    return result.stdout if capture else ""


def _prepare_site_checkout(site_root: Path) -> None:
    """Garante site limpo e actualizado antes de qualquer escrita no Spotify."""
    if not (site_root / "src" / "data" / "weeks").is_dir():
        raise typer.BadParameter(f"Site não encontrado em {site_root}", param_hint="--site-dir")
    if _git(site_root, "status", "--porcelain", capture=True).strip():
        console.print("O site tem alterações por publicar; resolve-as antes de continuar.")
        raise typer.Exit(code=1)
    _git(site_root, "pull", "--ff-only", "--quiet")


def _publish_site(site_root: Path, week: str) -> None:
    """Valida a build e publica apenas o JSON da semana no repositório do site."""
    relative = f"src/data/weeks/{week}.json"
    changed = {
        line[3:] for line in _git(site_root, "status", "--porcelain", capture=True).splitlines()
    }
    if changed - {relative}:
        console.print(f"Alterações inesperadas no site: {sorted(changed - {relative})}")
        raise typer.Exit(code=1)
    if not changed:
        console.print("Site já actualizado.")
        return
    if (site_root / "node_modules").is_dir():
        subprocess.run(["npm", "run", "build"], cwd=site_root, check=True)  # noqa: S607
    _git(site_root, "add", relative)
    _git(site_root, "commit", "--quiet", "-m", f"feat(data): publish {week}")
    _git(site_root, "push", "--quiet")


@app.command(hidden=True)
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


@app.command(hidden=True)
def feedback(
    uri: str | None = typer.Option(None, "--uri", help="Spotify URI a avaliar"),
    rating: str | None = typer.Option(None, "--rating", help="love|like|meh|skip|ban"),
    comment: str | None = typer.Option(None, "--comment", help="Comentário opcional"),
    history: bool = typer.Option(
        False, "--history", help="Avalia backlog histórico fora da triagem"
    ),
    week: str | None = typer.Option(
        None,
        "--week",
        help="Semana ISO do backlog (requer --history)",
    ),
    limit: int = typer.Option(28, "--limit", min=1, help="Número máximo de tracks"),
) -> None:
    """Avalia a triagem activa; use ``--history`` para o backlog histórico."""
    normalized_week: str | None = None
    if week is not None:
        if not history:
            typer.echo("Erro: --week requer --history.", err=True)
            raise typer.Exit(code=2)
        try:
            normalized_week = _normalize_week_option(week)
        except typer.BadParameter as exc:
            typer.echo(f"Erro: {exc}", err=True)
            raise typer.Exit(code=2) from exc

    _auto_sync_state("feedback")
    db_path = _resolve_path(settings.db_path)
    db = DB(str(db_path))
    try:
        db.init_schema()

        if uri is not None:
            chosen_rating = rating or _prompt_rating(default="like")
            _save_feedback(db, uri, chosen_rating, comment)
            console.print(f"Saved feedback: {uri} -> {chosen_rating}")
            return

        if history:
            _run_history_feedback_session(db, week=normalized_week, limit=limit)
        else:
            _run_triage_feedback_session(db, limit=limit)
    finally:
        db.close()


def _active_unrated_triage_items(db: DB) -> list[ReviewQueueItem]:
    """Tracks activas sem feedback, pela ordem exacta do snapshot Spotify."""
    playlist_id = settings.peel_review_playlist_id or settings.peel_playlist_id
    return [
        item
        for item in db.review_queue(playlist_id)
        if db.feedback_for_track_identity(item.spotify_uri) is None
    ]


def _run_triage_feedback_session(db: DB, *, limit: int, push_hint: bool = True) -> int:
    """Sessão interactiva canónica da fila activa de triagem; devolve avaliações gravadas."""
    playlist_id = settings.peel_review_playlist_id or settings.peel_playlist_id
    queue = db.review_queue(playlist_id)
    if not queue:
        console.print("Sem snapshot de triagem confirmado. Aguarda a próxima weekly.")
        return 0
    positions = {item.spotify_uri: index for index, item in enumerate(queue, 1)}

    items = _active_unrated_triage_items(db)
    if not items:
        console.print("Triagem activa completa: todas as tracks já foram avaliadas.")
        return 0

    interrupted = False
    saved_count = 0
    for index, item in enumerate(items[:limit], start=1):
        console.print(
            f"#{positions[item.spotify_uri]} · [{index}/{min(len(items), limit)}] "
            f"{item.artist} — {item.title} "
            f"({source_label(item.source_id)})"
        )
        chosen_rating = _prompt_rating(
            default="like",
            open_item=lambda uri=item.spotify_uri: _open_listen_url(_spotify_web_url(uri)),
        )
        if chosen_rating in {"q", "quit", "exit"}:
            interrupted = True
            break
        _save_feedback(db, item.spotify_uri, chosen_rating, _prompt_comment(default=""))
        saved_count += 1
        console.print(f"Saved: {item.artist} — {item.title} [{chosen_rating}]")

    remaining = len(_active_unrated_triage_items(db))
    if interrupted:
        console.print(f"Sessão interrompida. Faltam {remaining} tracks activas por avaliar.")
    elif remaining:
        console.print(f"Sessão terminada. Faltam {remaining} tracks activas por avaliar.")
    else:
        console.print("Triagem activa completa: todas as tracks já foram avaliadas.")
    if saved_count and push_hint:
        console.print("Corre uv run peel sync push.")
    return saved_count


def _active_triage_identities(db: DB) -> set[tuple[str, str]]:
    """Identidades normalizadas activas, incluindo variantes de URI."""
    playlist_id = settings.peel_review_playlist_id or settings.peel_playlist_id
    return {
        (normalize(item.artist), normalize(item.title)) for item in db.review_queue(playlist_id)
    }


def _run_history_feedback_session(db: DB, *, week: str | None, limit: int) -> None:
    """Sessão explícita do backlog histórico, excluindo a triagem activa."""
    exclude_identities = _active_triage_identities(db)
    rows = db.unrated_tracks(
        week=week,
        limit=limit,
        exclude_identities=exclude_identities,
    )
    if not rows:
        console.print("Sem tracks históricas por avaliar.")
        return

    interrupted = False
    saved_count = 0
    for index, row in enumerate(rows, start=1):
        _print_feedback_prompt(db, row, index, len(rows))
        chosen_rating = _prompt_rating(default="like")
        if chosen_rating in {"q", "quit", "exit"}:
            interrupted = True
            break
        chosen_comment = _prompt_comment(default="")
        _save_feedback(db, row[0], chosen_rating, chosen_comment)
        saved_count += 1
        console.print(f"Saved: {row[1]} — {row[2]} [{chosen_rating}]")

    remaining = bool(
        db.unrated_tracks(
            week=week,
            limit=1,
            exclude_identities=exclude_identities,
        )
    )
    if interrupted:
        console.print("Sessão histórica interrompida.")
    elif remaining:
        console.print("Sessão histórica terminada; ainda há tracks por avaliar.")
    else:
        console.print("Backlog histórico completo.")
    if saved_count:
        console.print("Corre uv run peel sync push.")


def _spotify_app_uri(url: str) -> str | None:
    """Convert a Spotify web URL to a URI understood by the desktop app."""
    if url.startswith("spotify:"):
        return url
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if (parsed.hostname or "").lower() != "open.spotify.com":
        return None
    parts = parsed.path.strip("/").split("/", 1)
    if len(parts) != 2 or not parts[1]:
        return None
    kind, value = parts
    if kind not in {"album", "search", "track", "playlist"}:
        return None
    return f"spotify:{kind}:{value}"


def _open_listen_url(url: str) -> None:
    """Prefer the installed Spotify app and keep the browser as fallback."""
    spotify_uri = _spotify_app_uri(url)
    spotify = shutil.which("spotify")
    if spotify_uri and spotify:
        try:
            subprocess.Popen(  # noqa: S603 - executable resolved locally with which()
                [spotify, f"--uri={spotify_uri}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
        except OSError:
            pass
    webbrowser.open(url)


@albums_app.callback(invoke_without_command=True)
def albums(
    ctx: typer.Context,
    unrated: Annotated[
        bool, typer.Option("--unrated", help="Mostra só álbuns por avaliar")
    ] = False,
    open_rank: Annotated[
        int | None, typer.Option("--open", min=1, help="Abre link de escuta do rank")
    ] = None,
    week: Annotated[
        str | None,
        typer.Option("--week", help="Semana ISO; por defeito usa a fila activa"),
    ] = None,
) -> None:
    """Mostra a fila persistida activa ou uma snapshot semanal explícita."""
    if ctx.invoked_subcommand is not None:
        if week is not None:
            raise typer.BadParameter(
                "Coloca --week depois do subcomando, por exemplo "
                "`albums feedback --week 2026-W32`.",
                param_hint="--week",
            )
        return

    target_week = _normalize_week_option(week) if week is not None else None
    _auto_sync_state("albums")
    db = DB(str(_resolve_path(settings.db_path)))
    try:
        db.init_schema()
        if target_week is not None:
            items = db.album_queue(target_week)
            if items is None:
                raise typer.BadParameter(
                    f"Sem snapshot canónica de álbuns para {target_week}.",
                    param_hint="--week",
                )
            if not items:
                console.print(f"A snapshot de álbuns {target_week} está vazia.")
                return
        else:
            items = db.latest_album_queue(
                settings.peel_review_playlist_id or settings.peel_playlist_id
            )
            if items is None:
                console.print("Sem fila de álbuns confirmada. Aguarda a próxima weekly.")
                return
            if not items:
                console.print("A fila de álbuns confirmada mais recente está vazia.")
                return
            target_week = items[0].week

        shown = [
            item
            for item in items
            if not unrated or db.album_feedback_for_identity(item.artist, item.album) is None
        ]
        if open_rank is not None:
            chosen = next((item for item in items if item.position == open_rank), None)
            if not chosen:
                raise typer.BadParameter(f"Não existe rank {open_rank} na fila {target_week}.")
            if not chosen.listen_url:
                console.print(f"Sem link de escuta para {chosen.artist} — {chosen.album}.")
                return
            _open_listen_url(chosen.listen_url)
            return
        if not shown:
            console.print(f"Sem álbuns {target_week} por avaliar.")
            return
        table = Table(title=f"Peel — Álbuns ({target_week})")
        table.add_column("#", justify="right")
        table.add_column("Estado")
        table.add_column("Artist", style="bold")
        table.add_column("Album")
        table.add_column("Sources")
        table.add_column("Listen")
        for item in shown:
            feedback = db.album_feedback_for_identity(item.artist, item.album)
            table.add_row(
                str(item.position),
                f"✓ {feedback[1]}" if feedback else "pendente",
                item.artist,
                item.album,
                str(item.source_count),
                item.listen_url or "",
            )
        console.print(table)
    finally:
        db.close()


@albums_app.command("refresh")
def albums_refresh(
    week: Annotated[str, typer.Option("--week", help="Semana ISO a reconstruir")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Mostra sem escrever")] = False,
    fetch_sources: Annotated[
        bool,
        typer.Option("--fetch", help="Actualiza apenas as sources de álbuns antes do ranking"),
    ] = False,
) -> None:
    """Reconstrói explicitamente uma snapshot sem weekly ou Telegram.

    Repetir ``--week`` substitui deliberadamente a snapshot dessa semana antes
    de publicar; uma re-exportação normal, pelo contrário, apenas a lê.
    """
    target_week = _normalize_week_option(week)
    current_week = iso_week(datetime.now(UTC))
    if fetch_sources and target_week != current_week:
        raise typer.BadParameter(
            "--fetch só é permitido para a semana actual; preserva point-in-time histórico."
        )
    _auto_sync_state("albums refresh")
    real_path = _resolve_path(settings.db_path)
    temp_path: str | None = None
    if dry_run:
        fd, temp_path = tempfile.mkstemp(prefix="peel-albums-refresh-", suffix=".db")
        os.close(fd)
        shutil.copyfile(real_path, temp_path)
        db_path = Path(temp_path)
    else:
        db_path = real_path
    db = DB(str(db_path))
    try:
        db.init_schema()
        if fetch_sources:
            try:
                discovery = discover_album_mentions(db)
            except AlbumDiscoveryError as exc:
                raise typer.BadParameter(str(exc), param_hint="--fetch") from exc
            console.print(
                f"Album sources: {discovery.sources}; fetched: {discovery.fetched}; "
                f"fresh: {discovery.fresh}; new: {discovery.new_albums}."
            )
        # Sunday is inside the target ISO week; using Monday would exclude
        # practically the entire target week from the same quality window the
        # weekly pipeline uses.
        reference = datetime.fromisocalendar(
            int(target_week[:4]), int(target_week[-2:]), 7
        ).replace(tzinfo=UTC)
        quality = {
            score.source_id: (score.avg_rating or 0.0, score.score)
            for score in build_source_scores(db, weeks=4, reference_dt=reference)
        }
        selected = select_album_queue(
            db,
            target_week,
            limit=MAX_ALBUM_RESOLUTION_CANDIDATES,
            source_quality=quality,
            affinity=build_affinity_profile(db).score,
        )
        if not selected and db.album_queue(target_week) is not None:
            console.print("Sem álbuns elegíveis; snapshot existente preservada.")
            return
        resolver = None
        if not dry_run:
            try:
                resolver = make_album_resolver(SpotifyClient())
            except SpotifyReauthRequired as exc:
                _abort_spotify_reauth(exc)
        existing = db.album_queue(target_week) or []
        listen_cache = {
            (item.artist_key, item.album_key): (item.listen_url, item.listen_kind)
            for item in existing
            if item.listen_url and item.listen_kind in {"spotify", "bandcamp"}
        }
        items = _album_queue_snapshot_items(
            target_week,
            selected,
            resolver,
            cached_listen_urls=listen_cache,
        )
        table = Table(title=f"Peel — Álbuns {target_week}")
        table.add_column("#", justify="right")
        table.add_column("Artist", style="bold")
        table.add_column("Album")
        table.add_column("Listen")
        for item in items:
            table.add_row(str(item.position), item.artist, item.album, item.listen_url or "")
        console.print(table)
        if len(items) < MAX_ALBUM_QUEUE_ITEMS:
            reason = (
                "dry-run sem pesquisa Spotify; a execução pode resolver mais links."
                if dry_run
                else "os restantes candidatos não têm link directo confirmado."
            )
            console.print(f"Fila incompleta: {len(items)}/{MAX_ALBUM_QUEUE_ITEMS}; {reason}")
        if dry_run:
            console.print("Dry run: snapshot não escrita.")
            return
        db.replace_album_queue(target_week, items)
        console.print(
            "Snapshot reconstruída. Corre uv run peel site export e uv run peel sync push."
        )
    finally:
        db.close()
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


@albums_app.command("feedback")
def albums_feedback(
    week: Annotated[
        str | None,
        typer.Option(
            "--week",
            help="Semana ISO histórica; por defeito usa a fila activa",
        ),
    ] = None,
) -> None:
    """Avalia álbuns pendentes da fila activa ou de uma semana explícita."""
    target_week = _normalize_week_option(week) if week is not None else None
    _auto_sync_state("albums feedback")
    db = DB(str(_resolve_path(settings.db_path)))
    try:
        db.init_schema()
        if target_week is not None:
            items = db.album_queue(target_week)
            if items is None:
                raise typer.BadParameter(
                    f"Sem snapshot canónica de álbuns para {target_week}.",
                    param_hint="--week",
                )
            if not items:
                console.print(f"A snapshot de álbuns {target_week} está vazia.")
                return
        else:
            items = db.latest_album_queue(
                settings.peel_review_playlist_id or settings.peel_playlist_id
            )
            if items is None:
                console.print("Sem fila de álbuns confirmada. Aguarda a próxima weekly.")
                return
            if not items:
                console.print("A fila de álbuns confirmada mais recente está vazia.")
                return
            target_week = items[0].week

        _run_album_feedback_session(db, items, target_week)
    finally:
        db.close()


def _run_album_feedback_session(
    db: DB, items: list[AlbumQueueItem], week: str, *, push_hint: bool = True
) -> int:
    """Avalia os álbuns pendentes de uma fila; devolve avaliações gravadas."""
    pending = [
        item for item in items if db.album_feedback_for_identity(item.artist, item.album) is None
    ]
    if not pending:
        console.print(f"Fila de álbuns {week} completa: todos foram avaliados.")
        return 0

    console.print(f"Feedback de álbuns: {week}")
    saved = 0
    for index, item in enumerate(pending, start=1):
        console.print(f"[{index}/{len(pending)}] {item.artist} — {item.album}")
        if item.listen_url:
            console.print(item.listen_url)
        open_album = (
            (lambda url=item.listen_url: _open_listen_url(url)) if item.listen_url else None
        )
        rating = _prompt_rating(
            default="like", ratings=ALBUM_FEEDBACK_RATINGS, open_item=open_album
        )
        if rating in {"q", "quit", "exit"}:
            break
        db.upsert_album_feedback(item.artist, item.album, rating, _prompt_comment(default=""))
        saved += 1
        console.print(f"Saved: {item.artist} — {item.album} [{rating}]")
    remaining = sum(
        db.album_feedback_for_identity(item.artist, item.album) is None for item in items
    )
    if remaining:
        console.print(f"Sessão {week} terminada. Faltam {remaining} álbuns por avaliar.")
    else:
        console.print(f"Fila de álbuns {week} completa: todos foram avaliados.")
    if saved and push_hint:
        console.print("Corre uv run peel sync push.")
    return saved


@triage_app.callback(invoke_without_command=True)
def triage(
    ctx: typer.Context,
    unrated: Annotated[
        bool,
        typer.Option(
            "--unrated",
            "--pending",
            help="Mostra só tracks activas sem avaliação",
        ),
    ] = False,
    open_playlist: Annotated[
        bool,
        typer.Option("--open", help="Abre a playlist Spotify confirmada"),
    ] = False,
) -> None:
    """Mostra a triagem exacta confirmada depois da última actualização Spotify."""
    if ctx.invoked_subcommand is not None:
        return

    _auto_sync_state("triage")
    playlist_id = settings.peel_review_playlist_id or settings.peel_playlist_id
    db = DB(str(_resolve_path(settings.db_path)))
    try:
        db.init_schema()
        queue_items = db.review_queue(playlist_id)
        items = [
            (position, item, db.feedback_for_track_identity(item.spotify_uri))
            for position, item in enumerate(queue_items, 1)
        ]
        if unrated:
            items = [
                (position, item, feedback) for position, item, feedback in items if feedback is None
            ]
    finally:
        db.close()

    if not items:
        if unrated:
            console.print("Sem tracks activas sem avaliação.")
        else:
            console.print("Sem snapshot de triagem confirmado. Aguarda a próxima weekly.")
        return

    table = Table(title=f"Peel — Triagem confirmada ({len(items)})")
    table.add_column("#", justify="right")
    table.add_column("Estado")
    table.add_column("Source")
    table.add_column("Artist", style="bold")
    table.add_column("Title")
    for index, item, feedback in items:
        if feedback is not None:
            state = f"✓ {feedback[1]}"
        else:
            state = f"🆕 {item.current_week}" if item.is_new else f"↻ {item.added_at_week}"
        table.add_row(
            str(index),
            state,
            source_label(item.source_id),
            item.artist,
            item.title,
        )
    console.print(table)

    if open_playlist:
        webbrowser.open(f"https://open.spotify.com/playlist/{playlist_id}")


@triage_app.command("feedback")
def triage_feedback(
    limit: Annotated[int, typer.Option("--limit", min=1, help="Máximo de tracks")] = 28,
) -> None:
    """Alias de compatibilidade para ``peel feedback``."""
    _auto_sync_state("triage feedback")
    db = DB(str(_resolve_path(settings.db_path)))
    try:
        db.init_schema()
        _run_triage_feedback_session(db, limit=limit)
    finally:
        db.close()


@triage_app.command("bootstrap")
def triage_bootstrap() -> None:
    """Guarda a playlist de triagem actual como snapshot local confirmado.

    Útil uma vez após instalar esta funcionalidade; futuras weeklys gravam a
    snapshot automaticamente depois do replace Spotify bem-sucedido.
    """
    _auto_sync_state("triage bootstrap")
    playlist_id = settings.peel_review_playlist_id or settings.peel_playlist_id
    try:
        client = SpotifyClient()
    except SpotifyReauthRequired as exc:
        _abort_spotify_reauth(exc)
    uris = client.playlist_track_uris(playlist_id)

    db = DB(str(_resolve_path(settings.db_path)))
    try:
        db.init_schema()
        scores = build_source_scores(db, weeks=4)
        source_quality = {
            score.source_id: (score.avg_rating or 0.0, score.score) for score in scores
        }
        items = build_triage_items(
            db,
            uris,
            set(),
            iso_week(datetime.now(UTC)),
            source_quality,
            build_affinity_profile(db),
        )
        if len(items) != len(uris):
            raise typer.BadParameter(
                "A playlist tem tracks sem detalhe local; não foi criado snapshot. "
                "Corre uma weekly ou confirma a DB sincronizada."
            )
        db.replace_review_queue(playlist_id, items, week=iso_week(datetime.now(UTC)))
    finally:
        db.close()

    console.print(f"Snapshot criado: {len(items)} tracks em {playlist_id}.")


@app.command(rich_help_panel=PANEL_BROWSE)
def report(
    week: Annotated[str | None, typer.Option(help="Semana ISO a gerar")] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(help="Directório de saída"),
    ] = None,
    html_output: Annotated[
        bool,
        typer.Option("--html", help="Gera também uma preview HTML local"),
    ] = False,
    open_report: Annotated[
        bool,
        typer.Option("--open", help="Gera e abre a preview HTML no browser"),
    ] = False,
    refresh_report: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help="Substitui explicitamente um Markdown histórico existente",
        ),
    ] = False,
) -> None:
    """Gera o relatório semanal sem reescrever snapshots históricos por defeito."""
    requested_week = _normalize_week_option(week) if week else None
    _auto_sync_state("report")
    db_path = _resolve_path(settings.db_path)
    db = DB(str(db_path))
    try:
        db.init_schema()
        active_week = db.active_review_week(
            settings.peel_review_playlist_id or settings.peel_playlist_id
        )
        target_week = requested_week or active_week or iso_week(datetime.now(UTC))
        target_dir = output_dir or PROJECT_ROOT / "data" / "reports"
        markdown_path = target_dir / f"{target_week}.md"
        canonical_week = active_week or latest_state_week(db_path)
        preserve_historical = (
            markdown_path.exists()
            and canonical_week is not None
            and target_week != canonical_week
            and not refresh_report
        )
        if db.is_archived_week(target_week):
            target_dir = target_dir / "archive"
            markdown_path = target_dir / f"{target_week}.md"
            preserve_historical = markdown_path.exists() and not refresh_report
        if preserve_historical:
            console.print(
                f"Report preserved: {markdown_path} (histórico; usa --refresh para substituir)."
            )
        else:
            try:
                markdown_path = generate_weekly_report(
                    db,
                    week=target_week,
                    output_dir=target_dir,
                )
            except ValueError as exc:
                raise typer.BadParameter(str(exc), param_hint="--week") from exc
            console.print(f"Report written: {markdown_path}")
        if html_output or open_report:
            html_path = generate_weekly_html_report(
                db,
                week=target_week,
                output_dir=target_dir,
            )
            console.print(f"HTML preview written: {html_path}")
            if open_report:
                webbrowser.open(html_path.resolve().as_uri())
    finally:
        db.close()


@app.command(rich_help_panel=PANEL_MAINTENANCE)
def sources(
    weeks: int = typer.Option(12, "--semanas", "--weeks", min=1, help="Janela em semanas"),
    detalhe: bool = typer.Option(False, "--detalhe", help="Tabela técnica de scoring"),
    min_tracks: int = typer.Option(
        0,
        "--min-tracks",
        min=0,
        help="Filtra sources com menos tracks matched",
    ),
    min_data_tracks: int = typer.Option(
        5,
        "--min-data-tracks",
        min=1,
        help="Tracks matched mínimas para mostrar score como confiável",
    ),
    json_output: bool = typer.Option(False, "--json", help="Saída JSON"),
) -> None:
    """Fontes: quanto trazem por semana, quanto acertam e se estão saudáveis."""
    if not json_output and not detalhe:
        _print_source_overview(weeks)
        return
    if json_output:
        with redirect_stdout(sys.stderr):
            rows = _source_score_rows(weeks, min_tracks)
        typer.echo(
            json.dumps(
                [_source_score_to_dict(row, min_data_tracks) for row in rows],
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
    table.add_column("Source", style="bold", no_wrap=True)
    table.add_column("Fnd", justify="right", header_style="bold", overflow="fold")
    table.add_column("Runs", justify="right", header_style="bold", overflow="fold")
    table.add_column("F/F", justify="right", header_style="bold", overflow="fold")
    table.add_column("Proc", justify="right", header_style="bold", overflow="fold")
    table.add_column("S/C/E", justify="right", header_style="bold", overflow="fold")
    table.add_column("Mat", justify="right", header_style="bold", overflow="fold")
    table.add_column("New", justify="right", header_style="bold", overflow="fold")
    table.add_column("Con", justify="right", header_style="bold", overflow="fold")
    table.add_column("Unm", justify="right", header_style="bold", overflow="fold")
    table.add_column("Like", justify="right", header_style="bold", overflow="fold")
    table.add_column("Skip", justify="right", header_style="bold", overflow="fold")
    table.add_column("Avg", justify="right", header_style="bold", overflow="fold")
    table.add_column("Data", justify="right", header_style="bold", overflow="fold")
    table.add_column("Score", justify="right", header_style="bold", overflow="fold")

    for row in rows:
        confidence = _source_confidence(row, min_data_tracks)
        score_display = "—" if confidence == "insufficient data" else f"{row.score:.1f}"
        table.add_row(
            row.source_id,
            str(row.tracks_found),
            str(row.run_count),
            f"{row.fetched_count}/{row.fresh_count}",
            str(row.processed_count),
            f"{row.skipped_stale_count}/{row.skipped_cap_count}/{row.error_count}",
            str(row.tracks_matched),
            str(row.new_unique_tracks),
            str(row.consensus_hits),
            str(row.unmatched_count),
            str(row.liked_count),
            str(row.skipped_count),
            row.avg_rating_display,
            confidence,
            score_display,
        )

    console.print(table)


def _print_source_overview(weeks: int) -> None:
    db = DB(str(_resolve_path(settings.db_path)))
    try:
        db.init_schema()
        specs = [(spec.source_id, spec.source_name, spec.create().kind) for spec in ACTIVE_SOURCES]
        rows = build_source_overview(db, specs, weeks=weeks)
    finally:
        db.close()
    styles = {"ok": "green", "pouco volume": "yellow"}
    for kind, title, unit in (("track", "Faixas", "faixas"), ("album", "Álbuns", "álbuns")):
        table = Table(title=f"{title} — últimas {weeks} semanas", title_justify="left")
        table.add_column("Fonte", style="bold")
        table.add_column(f"{unit}/semana", justify="right")
        table.add_column("Avaliadas", justify="right")
        table.add_column("Gostei", justify="right")
        table.add_column("Estado")
        for row in (r for r in rows if r.kind == kind):
            rate = f"{row.positive_rate:.0%}" if row.positive_rate is not None else "—"
            style = styles.get(row.status, "red")
            table.add_row(
                row.name,
                f"{row.per_week:.1f}",
                str(row.rated),
                rate,
                f"[{style}]{row.status}[/{style}]",
            )
        console.print(table)
    console.print(
        "Gostei = love/like nas avaliações das descobertas de cada fonte. "
        "Detalhe técnico: peel sources --detalhe"
    )


@site_app.command("export")
def site_export(
    site_dir: Annotated[
        Path,
        typer.Option("--site-dir", help="Diretório do site Astro peel-sept"),
    ] = Path("../peel-sept"),
    weeks: Annotated[int, typer.Option("--weeks", min=1, help="Nº de semanas a exportar")] = 2,
    week: Annotated[str | None, typer.Option("--week", help="Última semana do intervalo")] = None,
    playlist_id: Annotated[
        str | None,
        typer.Option("--playlist-id", help="ID/URI/URL da playlist Spotify"),
    ] = None,
    resolve_albums: Annotated[
        bool,
        typer.Option(
            "--resolve-albums/--no-resolve-albums",
            help="Procura cada álbum no Spotify para preencher o link (On Rotation)",
        ),
    ] = True,
) -> None:
    """Exporta JSON semanal para o site Astro peel-sept."""
    target_week = _normalize_week_option(week) if week else None
    _auto_sync_state("site export")
    album_resolver = None
    if resolve_albums:
        try:
            album_resolver = make_album_resolver(SpotifyClient())
        except SpotifyReauthRequired as exc:
            _abort_spotify_reauth(exc)
        except Exception as exc:  # noqa: BLE001 - sem Spotify, álbuns ficam com link editorial
            console.print(f"[yellow]Spotify indisponível; álbuns sem link Spotify: {exc}[/yellow]")

    db = DB(str(_resolve_path(settings.db_path)))
    try:
        db.init_schema()
        target_site_dir = _resolve_path(str(site_dir))
        exported = export_site(
            db,
            target_site_dir,
            weeks=weeks,
            playlist_id=playlist_id or settings.peel_playlist_id,
            current_week=target_week,
            album_resolver=album_resolver,
        )
    finally:
        db.close()

    if not exported:
        console.print("Sem semanas confirmadas para exportar.")
    for item in exported:
        console.print(f"Exported {item.week}: {item.path}")


@affinity_app.command("backfill-genres")
def affinity_backfill_genres(
    limit: Annotated[int, typer.Option("--limit", min=1, help="Máximo de artistas")] = 50,
    refresh_days: Annotated[
        int,
        typer.Option("--refresh-days", min=1, help="Só refaz cache mais antiga que N dias"),
    ] = 180,
    sleep_seconds: Annotated[
        float,
        typer.Option("--sleep", min=0.0, help="Pausa entre chamadas externas"),
    ] = 1.0,
    source: Annotated[
        str,
        typer.Option("--source", help="Fonte externa: musicbrainz ou spotify"),
    ] = "musicbrainz",
    min_tag_count: Annotated[
        int,
        typer.Option("--min-tag-count", min=0, help="Count mínimo para tags MusicBrainz"),
    ] = 1,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Não chama APIs externas")] = False,
) -> None:
    """Preenche a cache local artist_genres com throttle.

    Implementado para correr quando a rate limit acalmar; não é usado pela run
    semanal e nunca é chamado implicitamente.
    """
    source_name = source.strip().lower()
    if source_name not in {"musicbrainz", "spotify"}:
        raise typer.BadParameter("--source must be 'musicbrainz' or 'spotify'")

    db = DB(str(_resolve_path(settings.db_path)))
    try:
        db.init_schema()
        artists = db.artists_missing_genre_cache(refresh_days=refresh_days, limit=limit)
        if not artists:
            console.print("artist_genres já está fresco para os artistas conhecidos.")
            return

        console.print(f"Artistas pendentes: {len(artists)} ({source_name})")
        if dry_run:
            for artist in artists:
                console.print(f"- {artist}")
            return

        client: SpotifyClient | None = None
        if source_name == "spotify":
            try:
                client = SpotifyClient()
            except SpotifyReauthRequired as exc:
                _abort_spotify_reauth(exc)
        updated = 0
        failed = 0
        for index, artist in enumerate(artists, start=1):
            try:
                lookup = _lookup_artist_genres(
                    artist,
                    source_name=source_name,
                    spotify_client=client,
                    min_tag_count=min_tag_count,
                )
                if lookup is None:
                    failed += 1
                    console.print(f"[yellow][{index}/{len(artists)}] {artist}: no genres[/yellow]")
                    continue
                genres, external_id = lookup
                db.upsert_artist_genres(
                    artist,
                    genres,
                    source=source_name,
                    external_id=external_id,
                )
                updated += 1
                console.print(f"[{index}/{len(artists)}] {artist}: {', '.join(genres)}")
            except Exception as exc:  # noqa: BLE001 - backfill best-effort
                failed += 1
                console.print(f"[red][{index}/{len(artists)}] {artist}: {exc}[/red]")
            if sleep_seconds > 0 and index < len(artists):
                time.sleep(sleep_seconds)
        console.print(f"artist_genres updated={updated} failed={failed}")
    finally:
        db.close()


def _lookup_artist_genres(
    artist: str,
    *,
    source_name: str,
    spotify_client: SpotifyClient | None,
    min_tag_count: int,
) -> tuple[list[str], str | None] | None:
    if source_name == "musicbrainz":
        result = fetch_musicbrainz_artist_genres(artist, min_tag_count=min_tag_count)
        if result is None:
            return None
        return list(result.genres), result.mbid

    if spotify_client is None:
        raise RuntimeError("spotify client required for spotify genre backfill")
    result = spotify_client.sp.search(q=f'artist:"{artist}"', type="artist", limit=5)
    item = _select_artist_search_result(artist, result)
    if item is None:
        return None
    genres = [str(genre) for genre in (item.get("genres") or []) if str(genre).strip()]
    if not genres:
        return None
    return genres, item.get("id")


def _select_artist_search_result(artist: str, result: dict) -> dict | None:
    """Escolhe resultado só quando há match exacto normalizado.

    Backfill de géneros deve ser conservador: se a query por artista composto
    (`Drake Sexyy Red`, `Cobrah & Grimes`) devolver um artista individual como
    primeiro resultado, guardar esses géneros polui a cache.
    """
    items = ((result.get("artists") or {}).get("items") or []) if result else []
    if not items:
        return None
    target = normalize(artist)
    for item in items:
        if normalize(str(item.get("name", ""))) == target:
            return item
    return None


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _canonical_state_path() -> Path:
    return _project_path("data/peel.db").resolve()


def _uses_canonical_state() -> bool:
    return _resolve_path(settings.db_path).resolve() == _canonical_state_path()


def _auto_sync_state(command: str) -> None:
    """Refresh interactive state without merging or touching source code."""
    if _OFFLINE_MODE or _env_truthy("GITHUB_ACTIONS") or not _uses_canonical_state():
        return
    try:
        result = sync_remote_state(_canonical_state_path(), PROJECT_ROOT)
    except StateSyncError as exc:
        console.print(f"Estado canónico indisponível para `{command}`: {exc}")
        raise typer.Exit(code=1) from exc
    if result.status == "updated":
        before = result.previous_week or "sem estado"
        after = result.local_week or "sem semana"
        console.print(f"Estado sincronizado: {before} → {after}.")


@sync_app.command("status")
def sync_status() -> None:
    """Mostra separadamente o estado canónico e o checkout de código."""
    state = _git_sync_state()
    db_path = _canonical_state_path()
    try:
        local_changes = state_has_local_changes(db_path)
    except StateSyncError:
        local_changes = None

    table = Table(title="Git + canonical state sync")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Branch", state.branch)
    table.add_row("Upstream", state.upstream or "-")
    table.add_row("Dirty", _yes_no(state.dirty))
    table.add_row("Ahead", str(state.ahead))
    table.add_row("Behind", str(state.behind))
    table.add_row("data/peel.db changed vs Git", _yes_no(state.peel_db_changed))
    table.add_row("Canonical week", latest_state_week(db_path) or "-")
    table.add_row(
        "State changed since sync",
        "unknown" if local_changes is None else _yes_no(local_changes),
    )
    table.add_row(
        "Dirty paths",
        ", ".join(state.dirty_paths) if state.dirty_paths else "-",
    )
    console.print(Panel(table, title="Sync status"))


@sync_app.command("pull")
def sync_pull() -> None:
    """Sincroniza apenas a DB canónica; nunca faz merge do código."""
    try:
        result = sync_remote_state(_canonical_state_path(), PROJECT_ROOT)
    except StateSyncError as exc:
        console.print(str(exc))
        raise typer.Exit(code=1) from exc
    if result.status == "updated":
        console.print(
            f"State pull completed: {result.previous_week or '-'} → "
            f"{result.local_week or '-'}; backup: {result.backup_path}."
        )
    elif result.status == "local_changes":
        console.print("Estado remoto inalterado; a DB local contém alterações por enviar.")
    else:
        console.print(f"Estado já actual ({result.local_week or '-'}).")


@sync_app.command("push")
def sync_push() -> None:
    """Publica estado sobre o main remoto sem integrar código no checkout local."""
    changed = _push_state()
    console.print("Push completed." if changed else "Nothing to push.")


def _push_state() -> bool:
    """Envia feedback/estado local para o GitHub; ``Exit(1)`` se for inseguro."""
    db_path = _canonical_state_path()
    try:
        # Detecta primeiro o conflito real: feedback local + weekly remota nova.
        try:
            sync_result = sync_remote_state(db_path, PROJECT_ROOT)
        except LocalStateConflict:
            sync_result = merge_remote_state_for_push(db_path)
            console.print("Feedback local integrado sobre o estado remoto mais recente.")
        report_paths = _regenerate_state_reports(db_path)
        if not sync_result.remote_blob_sha:
            raise StateSyncError("Estado remoto sem SHA para push seguro.")
        changed = _push_state_checkout(
            db_path,
            report_paths,
            expected_remote_blob_sha=sync_result.remote_blob_sha,
        )
        mark_local_state_synced(db_path)
    except StateSyncError as exc:
        console.print(str(exc))
        raise typer.Exit(code=1) from exc
    return changed


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


def _source_confidence(row: SourceScore, min_data_tracks: int) -> str:
    """Label visual para evitar scores enganadores com pouca amostra."""
    return "ok" if row.tracks_matched >= min_data_tracks else "insufficient data"


def _source_score_to_dict(row: SourceScore, min_data_tracks: int) -> dict[str, object]:
    confidence = _source_confidence(row, min_data_tracks)
    return {
        "source_id": row.source_id,
        "tracks_found": row.tracks_found,
        "tracks_matched": row.tracks_matched,
        "run_count": row.run_count,
        "fetched_count": row.fetched_count,
        "fresh_count": row.fresh_count,
        "processed_count": row.processed_count,
        "skipped_stale_count": row.skipped_stale_count,
        "skipped_cap_count": row.skipped_cap_count,
        "error_count": row.error_count,
        "new_unique_tracks": row.new_unique_tracks,
        "duplicate_mentions": row.duplicate_mentions,
        "consensus_hits": row.consensus_hits,
        "unmatched_count": row.unmatched_count,
        "liked_count": row.liked_count,
        "skipped_count": row.skipped_count,
        "avg_rating": row.avg_rating,
        "confidence": confidence,
        "score": row.score,
        "score_display": "—" if confidence == "insufficient data" else f"{row.score:.1f}",
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


def _normalize_week_option(week: str) -> str:
    value = week.strip().upper()
    parts = value.split("-W")
    if len(parts) != 2:
        raise typer.BadParameter(f"Invalid ISO week: {week!r}. Expected YYYY-Www")
    try:
        year = int(parts[0])
        week_number = int(parts[1])
    except ValueError as exc:
        raise typer.BadParameter(f"Invalid ISO week: {week!r}. Expected YYYY-Www") from exc
    if not 1 <= week_number <= 53:
        raise typer.BadParameter("ISO week must be between 1 and 53")
    try:
        datetime.fromisocalendar(year, week_number, 1)
    except ValueError as exc:
        raise typer.BadParameter(f"Invalid ISO week: {week!r}") from exc
    return f"{year:04d}-W{week_number:02d}"


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _abort_spotify_reauth(exc: SpotifyReauthRequired) -> None:
    console.print("[red]Spotify reauthorization required.[/red]")
    console.print(str(exc))
    raise typer.Exit(code=1)


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


def _prompt_rating(
    default: str,
    ratings: Mapping[str, int] | None = None,
    open_item: Callable[[], None] | None = None,
) -> str:
    ratings = ratings or FEEDBACK_RATINGS
    ordered = [label for label in RATING_ORDER if label in ratings]
    ordered += sorted(label for label in ratings if label not in RATING_ORDER)
    shortcuts = {str(index): label for index, label in enumerate(ordered, start=1)}
    allowed = " · ".join(f"{index} {label}" for index, label in shortcuts.items())
    if open_item is not None:
        allowed += " · o abrir"
    while True:
        try:
            value = typer.prompt(f"Rating [{allowed} / q]", default=default).strip().lower()
        except UnicodeDecodeError:
            _configure_stdin_decode_errors()
            console.print("Input inválido recebido. Tenta outra vez.")
            continue
        if value in {"q", "quit", "exit"}:
            return value
        if value == "o" and open_item is not None:
            open_item()
            continue
        value = shortcuts.get(value, value)
        if value in ratings:
            return value
        console.print(f"Rating inválida: {value}")


def _prompt_comment(default: str) -> str | None:
    while True:
        try:
            value = typer.prompt("Comment optional", default=default, show_default=False)
        except UnicodeDecodeError:
            _configure_stdin_decode_errors()
            console.print("Comentário tinha bytes inválidos. Tenta outra vez ou carrega Enter.")
            continue
        return value.strip() or None


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


def _regenerate_state_reports(db_path: Path) -> list[Path]:
    """Regenerate the active edition (possibly recovered); leave other weeks frozen."""
    week = latest_state_week(db_path)
    if week is None or week < CANONICAL_ALBUM_QUEUE_SINCE:
        return []

    reports_dir = _project_path("data/reports")
    db = DB(str(db_path))
    try:
        db.init_schema()
        week = (
            db.active_review_week(settings.peel_review_playlist_id or settings.peel_playlist_id)
            or week
        )
        if db.is_archived_week(week):
            return []
        try:
            return [generate_weekly_report(db, week=week, output_dir=reports_dir)]
        except ValueError as exc:
            console.print(f"Report {week} não regenerado: {exc}")
            return []
    finally:
        db.close()


def _push_state_checkout(
    db_path: Path,
    report_paths: list[Path],
    *,
    expected_remote_blob_sha: str,
) -> bool:
    """Commit state from a temporary latest-main checkout, never local code."""
    origin_result = _run_git(["remote", "get-url", "--push", "origin"])
    if origin_result.returncode != 0:
        raise StateSyncError(_git_result_message(origin_result))
    push_url = origin_result.stdout.strip()
    if not push_url:
        raise StateSyncError("Git origin sem push URL.")
    _assert_state_push_target(push_url)

    with tempfile.TemporaryDirectory(prefix="peel-state-push-") as raw_dir:
        checkout = Path(raw_dir) / "repo"
        clone = _run_git_in(
            [
                "clone",
                "--quiet",
                "--depth",
                "1",
                "--branch",
                "main",
                STATE_CLONE_URL,
                str(checkout),
            ],
            PROJECT_ROOT,
        )
        if clone.returncode != 0:
            raise StateSyncError(_git_result_message(clone))

        target_data = checkout / "data"
        target_data.mkdir(parents=True, exist_ok=True)
        cloned_db = target_data / "peel.db"
        if not cloned_db.exists() or git_blob_sha(cloned_db) != expected_remote_blob_sha:
            raise StateSyncError(
                "O estado remoto avançou durante o push; nada foi enviado. "
                "Repete `uv run peel sync push` para refazer o merge."
            )
        shutil.copy2(db_path, cloned_db)
        target_reports = target_data / "reports"
        target_reports.mkdir(parents=True, exist_ok=True)
        for report_path in report_paths:
            shutil.copy2(report_path, target_reports / report_path.name)
        archived_db = DB(str(cloned_db))
        try:
            archive_report_files(archived_db, target_reports)
        except ValueError as exc:
            raise StateSyncError(str(exc)) from exc
        finally:
            archived_db.close()

        for args in (
            ["config", "user.name", "peel-local"],
            ["config", "user.email", "peel-local@users.noreply.github.com"],
            ["remote", "set-url", "--push", "origin", push_url],
            ["add", "data/peel.db", "data/reports"],
        ):
            result = _run_git_in(args, checkout)
            if result.returncode != 0:
                raise StateSyncError(_git_result_message(result))

        diff = _run_git_in(["diff", "--cached", "--quiet"], checkout)
        if diff.returncode == 0:
            return False
        if diff.returncode != 1:
            raise StateSyncError(_git_result_message(diff))
        commit = _run_git_in(["commit", "-m", "chore: update peel local feedback/state"], checkout)
        if commit.returncode != 0:
            raise StateSyncError(_git_result_message(commit))
        push = _run_git_in(["push", "origin", "HEAD:main"], checkout)
        if push.returncode != 0:
            raise StateSyncError(_git_result_message(push))
    return True


def _assert_state_push_target(push_url: str) -> None:
    if push_url.startswith("git@github.com:"):
        repository = push_url.split(":", 1)[1]
    else:
        try:
            parsed = urlparse(push_url)
        except ValueError as exc:
            raise StateSyncError(f"Push URL inválido: {push_url}") from exc
        if parsed.hostname != "github.com":
            raise StateSyncError("Push de estado recusado: origin não aponta para GitHub.")
        repository = parsed.path.lstrip("/")
    if repository.removesuffix(".git") != STATE_REPOSITORY:
        raise StateSyncError(
            f"Push de estado recusado: origin não corresponde a {STATE_REPOSITORY}."
        )


def _run_git_in(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _git_result_message(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "git command failed").strip()


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
