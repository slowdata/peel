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
import time
import tomllib
import webbrowser
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from peel.config import settings
from peel.db import DB, FEEDBACK_RATINGS, iso_week
from peel.doctor_sources import inspect_registered_sources
from peel.main import run as run_pipeline
from peel.matcher import normalize
from peel.musicbrainz import fetch_musicbrainz_artist_genres
from peel.release_radar import (
    DEFAULT_RELEASE_RADAR_URL,
    ReleaseRadarTrack,
    fetch_release_radar,
    release_radar_snapshot_payload,
    tracks_from_snapshot,
)
from peel.report import generate_weekly_report
from peel.scoring import SourceScore, build_source_scores
from peel.site_export import export_site, make_album_resolver
from peel.spotify_client import SpotifyClient, SpotifyReauthRequired

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _configure_stdin_decode_errors() -> None:
    """Evita crashes em prompts quando o terminal envia bytes UTF-8 inválidos."""
    reconfigure = getattr(sys.stdin, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="replace")


_configure_stdin_decode_errors()

console = Console(width=160)
app = typer.Typer(add_completion=False, help="Peel — música curada, sincronizada e visível.")
sync_app = typer.Typer(add_completion=False, help="Sincronização local/GitHub.")
doctor_app = typer.Typer(add_completion=False, help="Diagnósticos do Peel.")
playlist_app = typer.Typer(add_completion=False, help="Ferramentas de playlists Spotify.")
radar_app = typer.Typer(add_completion=False, help="Snapshots do Spotify Release Radar.")
site_app = typer.Typer(add_completion=False, help="Exportação para o site peel-sept.")
affinity_app = typer.Typer(add_completion=False, help="Perfil local de afinidade.")
app.add_typer(sync_app, name="sync")
app.add_typer(doctor_app, name="doctor")
app.add_typer(playlist_app, name="playlist")
app.add_typer(radar_app, name="radar")
app.add_typer(site_app, name="site")
app.add_typer(affinity_app, name="affinity")


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
def run_command(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Simula a run e envia o digest, sem escrever na DB nem nas playlists",
        ),
    ] = False,
) -> None:
    """Executa a pipeline semanal do Peel (use --dry-run para pré-visualizar sem impacto)."""
    try:
        run_pipeline(dry_run=dry_run)
    except SpotifyReauthRequired as exc:
        _abort_spotify_reauth(exc)


@app.command("finalize")
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
) -> None:
    """Publica a semana: keepers (love/like) da triagem → playlist Weekly + export do site."""
    target_week = week or iso_week(datetime.now(UTC))
    db = DB(str(_resolve_path(settings.db_path)))
    try:
        db.init_schema()
        keepers = db.week_keeper_uris(target_week)
        try:
            sp = SpotifyClient()
        except SpotifyReauthRequired as exc:
            _abort_spotify_reauth(exc)
        sp.replace_playlist_items(settings.peel_playlist_id, keepers)
        console.print(
            f"Finalized {target_week}: {len(keepers)} keepers → {settings.peel_playlist_id}"
        )
        if export:
            export_site(
                db,
                _resolve_path(str(site_dir)),
                weeks=2,
                playlist_id=settings.peel_playlist_id,
                album_resolver=make_album_resolver(sp),
            )
            console.print("Site exported.")
    finally:
        db.close()


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
    min_data_tracks: int = typer.Option(
        5,
        "--min-data-tracks",
        min=1,
        help="Tracks matched mínimas para mostrar score como confiável",
    ),
    json_output: bool = typer.Option(False, "--json", help="Saída JSON"),
) -> None:
    """Mostra scoring das sources com base nos dados existentes."""
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


@playlist_app.command("fill-week")
def playlist_fill_week(
    week: Annotated[str, typer.Argument(help="Semana ISO, ex. 2026-W22")],
    playlist_id: Annotated[str, typer.Option("--playlist-id", help="Playlist Spotify destino")],
    unrated_only: Annotated[
        bool,
        typer.Option("--unrated-only", help="Inclui só tracks ainda sem feedback"),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Mostra sem alterar Spotify")] = False,
) -> None:
    """Preenche uma playlist existente com tracks de uma semana."""
    normalized_week = _normalize_week_option(week)
    rows = _weekly_playlist_rows(normalized_week, unrated_only=unrated_only)
    if not rows:
        console.print(f"Sem tracks para {normalized_week}.")
        return

    table = Table(title=f"Playlist {normalized_week}")
    table.add_column("Artist", style="bold")
    table.add_column("Title")
    table.add_column("URI")
    for spotify_uri, artist, title in rows:
        table.add_row(artist, title, spotify_uri)
    console.print(table)

    if dry_run:
        console.print(f"Dry run: {len(rows)} tracks; playlist not changed.")
        return

    try:
        client = SpotifyClient()
    except SpotifyReauthRequired as exc:
        _abort_spotify_reauth(exc)
    client.replace_playlist_items(playlist_id, [row[0] for row in rows])
    console.print(f"Playlist filled: {playlist_id} ({len(rows)} tracks).")


@radar_app.command("snapshot")
def radar_snapshot(
    url: Annotated[
        str,
        typer.Option("--url", help="URL da playlist Release Radar no Spotify Web"),
    ] = DEFAULT_RELEASE_RADAR_URL,
    week: Annotated[str | None, typer.Option("--week", help="Semana ISO")] = None,
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Directório para snapshots JSON"),
    ] = Path("data/radar"),
    no_write: Annotated[
        bool,
        typer.Option("--no-write", help="Só mostra; não grava JSON"),
    ] = False,
) -> None:
    """Extrai a Release Radar via Spotify Web e grava snapshot local."""
    target_week = _normalize_week_option(week) if week else iso_week(datetime.now(UTC))
    tracks = fetch_release_radar(url)
    if not tracks:
        console.print("[red]Não consegui extrair tracks da página Spotify.[/red]")
        raise typer.Exit(code=1)

    _print_release_radar_tracks(tracks, title=f"Release Radar {target_week}")

    if no_write:
        console.print(f"Snapshot não gravado: {len(tracks)} tracks.")
        return

    target_dir = _resolve_path(str(output_dir))
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{target_week}.json"
    payload = release_radar_snapshot_payload(tracks, week=target_week, url=url)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    console.print(f"Snapshot written: {path} ({len(tracks)} tracks).")


@radar_app.command("liked")
def radar_liked(
    week: Annotated[str | None, typer.Option("--week", help="Semana ISO")] = None,
    snapshot: Annotated[
        Path | None,
        typer.Option("--snapshot", help="Snapshot JSON; default data/radar/<week>.json"),
    ] = None,
    show_all: Annotated[
        bool,
        typer.Option("--all", help="Mostra também tracks ainda não guardadas nos Liked Songs"),
    ] = False,
) -> None:
    """Mostra tracks da Release Radar guardadas nos Spotify Liked Songs."""
    target_week = _normalize_week_option(week) if week else iso_week(datetime.now(UTC))
    tracks = _load_release_radar_snapshot(target_week, snapshot)

    try:
        client = SpotifyClient()
    except SpotifyReauthRequired as exc:
        _abort_spotify_reauth(exc)

    saved = _spotify_saved_status(client, tracks)
    liked_count = sum(saved)

    table = Table(title=f"Release Radar liked {target_week}")
    table.add_column("#", justify="right")
    table.add_column("Liked")
    table.add_column("Artist", style="bold")
    table.add_column("Title")
    table.add_column("URI")
    for track, is_saved in zip(tracks, saved, strict=True):
        if not show_all and not is_saved:
            continue
        table.add_row(
            str(track.position),
            "yes" if is_saved else "",
            track.artist,
            track.title,
            track.spotify_uri,
        )
    console.print(table)
    console.print(f"Liked in Spotify: {liked_count}/{len(tracks)}")


@radar_app.command("compare")
def radar_compare(
    week: Annotated[str | None, typer.Option("--week", help="Semana ISO")] = None,
    snapshot: Annotated[
        Path | None,
        typer.Option("--snapshot", help="Snapshot JSON; default data/radar/<week>.json"),
    ] = None,
) -> None:
    """Compara uma snapshot Release Radar com tracks conhecidas no Peel."""
    target_week = _normalize_week_option(week) if week else iso_week(datetime.now(UTC))
    tracks = _load_release_radar_snapshot(target_week, snapshot)

    db = DB(str(_resolve_path(settings.db_path)))
    try:
        db.init_schema()
        matches = _compare_release_radar_tracks(db, tracks)
    finally:
        db.close()

    uri_matches = sum(1 for item in matches if item[0] == "uri")
    text_matches = sum(1 for item in matches if item[0] == "text")
    misses = sum(1 for item in matches if item[0] == "miss")

    table = Table(title=f"Release Radar overlap {target_week}")
    table.add_column("#", justify="right")
    table.add_column("Match")
    table.add_column("Artist", style="bold")
    table.add_column("Title")
    table.add_column("Peel sources")
    for status, track, sources in matches:
        label = {"uri": "URI", "text": "text", "miss": "—"}[status]
        style = "green" if status == "uri" else "yellow" if status == "text" else "dim"
        table.add_row(
            str(track.position),
            f"[{style}]{label}[/{style}]",
            track.artist,
            track.title,
            sources or "",
        )
    console.print(table)
    console.print(
        f"Overlap: {uri_matches + text_matches}/{len(tracks)} "
        f"(uri={uri_matches}, text={text_matches}, miss={misses})"
    )


@site_app.command("export")
def site_export(
    site_dir: Annotated[
        Path,
        typer.Option("--site-dir", help="Diretório do site Astro peel-sept"),
    ] = Path("../peel-sept"),
    weeks: Annotated[int, typer.Option("--weeks", min=1, help="Nº de semanas a exportar")] = 2,
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
            album_resolver=album_resolver,
        )
    finally:
        db.close()

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


def _weekly_playlist_rows(
    week: str,
    unrated_only: bool = False,
) -> list[tuple[str, str, str]]:
    db = DB(str(_resolve_path(settings.db_path)))
    try:
        db.init_schema()
        if unrated_only:
            rows = db.conn.execute(
                """
                SELECT t.spotify_uri, t.artist, t.title, MAX(t.added_at) AS last_added_at
                FROM tracks t
                LEFT JOIN feedback f ON f.spotify_uri = t.spotify_uri
                WHERE t.added_at_week = ?
                  AND f.spotify_uri IS NULL
                  AND NOT EXISTS (
                    SELECT 1
                    FROM tracks rated_track
                    JOIN feedback rated_feedback
                      ON rated_feedback.spotify_uri = rated_track.spotify_uri
                    WHERE rated_track.artist = t.artist
                      AND rated_track.title = t.title
                  )
                GROUP BY t.spotify_uri, t.artist, t.title
                ORDER BY last_added_at DESC, t.artist COLLATE NOCASE, t.title COLLATE NOCASE
                """,
                (week,),
            ).fetchall()
        else:
            rows = db.conn.execute(
                """
                SELECT spotify_uri, artist, title, MAX(added_at) AS last_added_at
                FROM tracks
                WHERE added_at_week = ?
                GROUP BY spotify_uri, artist, title
                ORDER BY last_added_at DESC, artist COLLATE NOCASE, title COLLATE NOCASE
                """,
                (week,),
            ).fetchall()
        return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]
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
        try:
            value = typer.prompt(f"Rating [{allowed} / q]", default=default).strip().lower()
        except UnicodeDecodeError:
            _configure_stdin_decode_errors()
            console.print("Input inválido recebido. Tenta outra vez.")
            continue
        if value in {"q", "quit", "exit"}:
            return value
        if value in FEEDBACK_RATINGS:
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


def _load_release_radar_snapshot(
    week: str,
    snapshot: Path | None = None,
) -> list[ReleaseRadarTrack]:
    path = (
        _resolve_path(str(snapshot))
        if snapshot
        else PROJECT_ROOT / "data" / "radar" / f"{week}.json"
    )
    if not path.exists():
        console.print(f"[red]Snapshot não existe:[/red] {path}")
        raise typer.Exit(code=1)

    payload = json.loads(path.read_text(encoding="utf-8"))
    tracks = tracks_from_snapshot(payload)
    if not tracks:
        console.print(f"[red]Snapshot sem tracks válidas:[/red] {path}")
        raise typer.Exit(code=1)
    return tracks


def _spotify_saved_status(
    client: SpotifyClient,
    tracks: list[ReleaseRadarTrack],
) -> list[bool]:
    uris = [track.spotify_uri for track in tracks]
    saved: list[bool] = []
    for index in range(0, len(uris), 50):
        saved.extend(client.sp.current_user_saved_tracks_contains(uris[index : index + 50]))
    return saved


def _print_release_radar_tracks(tracks: list[ReleaseRadarTrack], *, title: str) -> None:
    table = Table(title=title)
    table.add_column("#", justify="right")
    table.add_column("Artist", style="bold")
    table.add_column("Title")
    table.add_column("URI")
    for track in tracks:
        table.add_row(str(track.position), track.artist, track.title, track.spotify_uri)
    console.print(table)


def _compare_release_radar_tracks(
    db: DB,
    tracks: list[ReleaseRadarTrack],
) -> list[tuple[str, ReleaseRadarTrack, str | None]]:
    rows = db.conn.execute(
        """
        SELECT spotify_uri,
               MIN(artist) AS artist,
               MIN(title) AS title,
               GROUP_CONCAT(DISTINCT source_id) AS sources
        FROM tracks
        GROUP BY spotify_uri
        """
    ).fetchall()
    by_uri: dict[str, str | None] = {}
    by_text: dict[tuple[str, str], str | None] = {}
    for spotify_uri, artist, title, sources in rows:
        by_uri[str(spotify_uri)] = str(sources) if sources is not None else None
        key = (normalize(str(artist)), normalize(str(title)))
        by_text.setdefault(key, str(sources) if sources is not None else None)

    matches: list[tuple[str, ReleaseRadarTrack, str | None]] = []
    for track in tracks:
        uri_sources = by_uri.get(track.spotify_uri)
        if uri_sources is not None:
            matches.append(("uri", track, uri_sources))
            continue
        text_sources = by_text.get((normalize(track.artist), normalize(track.title)))
        if text_sources is not None:
            matches.append(("text", track, text_sources))
            continue
        matches.append(("miss", track, None))
    return matches


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
