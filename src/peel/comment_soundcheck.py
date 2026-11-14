"""Human-only preview for optional local comment polishing; never a Git hook."""

from __future__ import annotations

import difflib
import os
import sqlite3
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.text import Text

from peel.comment_polish import (
    CommentConflict,
    CommentRef,
    Suggestion,
    apply_comments,
    suggest,
)
from peel.language_tool import LocalLanguageTool


def interactive() -> bool:
    return (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.environ.get("CI", "").lower() not in {"1", "true"}
        and os.environ.get("GITHUB_ACTIONS", "").lower() not in {"1", "true"}
    )


def _diff(before: str, after: str) -> tuple[Text, Text]:
    original, suggestion = Text(), Text()
    for operation, a, b, c, d in difflib.SequenceMatcher(a=before, b=after).get_opcodes():
        original.append(before[a:b], style="bold red" if operation != "equal" else None)
        suggestion.append(after[c:d], style="bold green" if operation != "equal" else None)
    return original, suggestion


def _display(console: Console, index: int, item: Suggestion) -> None:
    console.print(Text(f"{index}. {item.ref.artist} — {item.ref.title}", style="bold"))
    before, after = _diff(item.ref.comment, item.after)
    console.print(Text("Original: ", style="bold") + before)
    if item.after != item.ref.comment:
        console.print(Text("Suggestion: ", style="bold") + after)
    if item.reason:
        console.print(Text(item.reason, style="yellow"))


def _choose(item: Suggestion) -> str | None:
    while True:
        action = (
            typer.prompt(
                "Apply [a] / Edit [e] / Keep original [k] / Finish [q]",
                default="k",
                show_default=True,
            )
            .strip()
            .lower()
        )
        if action in {"k", "keep"}:
            return None
        if action in {"q", "finish"}:
            return "\0"  # Internal sentinel, never stored as comment text.
        if action in {"a", "apply"} and item.after != item.ref.comment:
            return item.after
        if action in {"e", "edit"}:
            edited = typer.prompt("Comment", default=item.ref.comment, show_default=False).strip()
            return edited if edited and edited != item.ref.comment else None


def soundcheck(
    db_path: Path, refs: list[CommentRef], console: Console, *, offer: bool = True
) -> int:
    """A failed/offline/cancelled corrector never discards or blocks saved votes."""
    if not refs or not interactive():
        return 0
    try:
        if offer and not typer.confirm("Comment soundcheck before sync?", default=True):
            return 0
        console.print("[bold]Comment soundcheck[/bold]")
        console.print("Afino o português. A opinião continua por tua conta.")
        console.print("Checking locally · PT-PT · no cloud")
        proposals = []
        with LocalLanguageTool() as engine:
            deadline = time.monotonic() + 20
            for ref in refs[:150]:
                if time.monotonic() > deadline:
                    raise TimeoutError("Local comment batch exceeded its time budget")
                item = suggest(ref, [])
                if not item.needs_review:
                    item = suggest(ref, engine.check(ref.comment))
                if item.after != ref.comment or item.needs_review:
                    proposals.append(item)
        if len(refs) > 150:
            console.print("O lote foi limitado a 150 comentários; os restantes ficam como estão.")
        if not proposals:
            console.print("Sem desafinações detetadas. Segue o groove.")
            return 0
        for index, item in enumerate(proposals, 1):
            _display(console, index, item)
        action = (
            typer.prompt(
                "Review [r] / Apply safe [a] / Keep originals [k]", default="r", show_default=True
            )
            .strip()
            .lower()
        )
        while action not in {"r", "a", "k"}:
            action = typer.prompt("Choose r, a or k", default="k").strip().lower()
        if action == "k":
            return 0
        changes = []
        for index, item in enumerate(proposals, 1):
            if action == "a" and not item.needs_review:
                changes.append((item.ref, item.after))
                continue
            _display(console, index, item)
            edited = _choose(item)
            if edited == "\0":
                break
            if edited is not None:
                changes.append((item.ref, edited))
        backup = apply_comments(db_path, changes)
        if backup is None:
            console.print("Originais mantidos. O groove não sofreu alterações.")
            return 0
        console.print(f"[bold]Comments polished: {len(changes)}[/bold]")
        console.print("Ortografia afinada. Notas e datas intactas.")
        console.print(Text(f"Undo backup: {backup}"))
        return len(changes)
    except (KeyboardInterrupt, EOFError, typer.Abort):
        console.print("Revisão cancelada. As avaliações continuam guardadas.")
        return 0
    except (CommentConflict, OSError, sqlite3.Error):
        console.print(
            "Não gravei a revisão: o texto mudou ou não foi possível guardar com segurança."
        )
        console.print("As avaliações continuam guardadas; não vou repetir a escrita.")
        return 0
    except Exception as exc:
        # No raw HTTP errors, response bodies or comments in failure messages.
        console.print(
            Text(
                f"Corretor indisponível ({type(exc).__name__}). "
                "Originais guardados; o envio pode continuar."
            )
        )
        console.print("Setup: uv run peel comments setup")
        return 0
