from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

import peel.cli as cli

runner = CliRunner()


def _completed(
    args: list[str],
    returncode: int,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class TestSyncStatus:
    def test_sync_status_parses_git_porcelain(self, monkeypatch, tmp_path: Path) -> None:
        calls: list[list[str]] = []

        def fake_run(
            args: list[str],
            cwd: Path | None = None,
            capture_output: bool | None = None,
            text: bool | None = None,
        ) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return _completed(
                args,
                0,
                stdout=(
                    "## main...origin/main [ahead 1, behind 2]\n"
                    " M src/peel/cli.py\n"
                    "?? data/peel.db\n"
                ),
            )

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)

        result = runner.invoke(cli.app, ["sync", "status"])

        assert result.exit_code == 0
        assert "main" in result.stdout
        assert "origin/main" in result.stdout
        assert "yes" in result.stdout
        assert "1" in result.stdout
        assert "2" in result.stdout
        assert "data/peel.db" in result.stdout
        assert calls == [["git", "status", "--porcelain=v1", "-b"]]


class TestSyncPull:
    def test_sync_pull_aborts_when_dirty(self, monkeypatch, tmp_path: Path) -> None:
        calls: list[list[str]] = []

        def fake_run(
            args: list[str],
            cwd: Path | None = None,
            capture_output: bool | None = None,
            text: bool | None = None,
        ) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return _completed(args, 0, stdout="## main...origin/main\n M data/peel.db\n")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)

        result = runner.invoke(cli.app, ["sync", "pull"])

        assert result.exit_code == 1
        assert "dirty" in result.stdout.lower()
        assert calls == [["git", "status", "--porcelain=v1", "-b"]]


class TestSyncPush:
    def test_sync_push_aborts_when_staged_files_escape_scope(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        calls: list[list[str]] = []

        def fake_run(
            args: list[str],
            cwd: Path | None = None,
            capture_output: bool | None = None,
            text: bool | None = None,
        ) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args == ["git", "diff", "--cached", "--name-only"]:
                return _completed(args, 0, stdout="README.md\ndata/peel.db\n")
            raise AssertionError(f"Unexpected git command: {args}")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)

        result = runner.invoke(cli.app, ["sync", "push"])

        assert result.exit_code == 1
        assert "Staged files outside Peel sync scope" in result.stdout
        assert "README.md" in result.stdout
        assert calls == [["git", "diff", "--cached", "--name-only"]]

    def test_sync_push_stages_commits_and_pushes(self, monkeypatch, tmp_path: Path) -> None:
        repo_root = tmp_path
        (repo_root / "data" / "reports").mkdir(parents=True)
        (repo_root / "data" / "peel.db").write_text("db", encoding="utf-8")
        (repo_root / "data" / "reports" / "2026-W18.md").write_text("report", encoding="utf-8")

        calls: list[list[str]] = []

        def fake_run(
            args: list[str],
            cwd: Path | None = None,
            capture_output: bool | None = None,
            text: bool | None = None,
        ) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args == ["git", "diff", "--cached", "--name-only"]:
                return _completed(args, 0, stdout="")
            if args == ["git", "status", "--porcelain=v1", "-b"]:
                return _completed(args, 0, stdout="## main...origin/main\n")
            if args[:2] == ["git", "add"]:
                return _completed(args, 0)
            if args == ["git", "diff", "--cached", "--quiet"]:
                return _completed(args, 1)
            if args[:2] == ["git", "commit"]:
                return _completed(args, 0)
            if args[:2] == ["git", "push"]:
                return _completed(args, 0)
            raise AssertionError(f"Unexpected git command: {args}")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        monkeypatch.setattr(cli, "PROJECT_ROOT", repo_root)

        result = runner.invoke(cli.app, ["sync", "push"])

        assert result.exit_code == 0
        assert "Push completed" in result.stdout
        assert calls[0] == ["git", "diff", "--cached", "--name-only"]
        assert calls[1] == ["git", "status", "--porcelain=v1", "-b"]
        assert calls[2] == ["git", "add", "data/peel.db", "data/reports"]
        assert calls[3] == ["git", "diff", "--cached", "--quiet"]
        assert calls[4][:2] == ["git", "commit"]
        assert calls[5] == ["git", "push"]

    def test_sync_push_pushes_when_branch_is_ahead_without_new_changes(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        repo_root = tmp_path
        (repo_root / "data").mkdir(parents=True)
        (repo_root / "data" / "peel.db").write_text("db", encoding="utf-8")

        calls: list[list[str]] = []

        def fake_run(
            args: list[str],
            cwd: Path | None = None,
            capture_output: bool | None = None,
            text: bool | None = None,
        ) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args == ["git", "diff", "--cached", "--name-only"]:
                return _completed(args, 0, stdout="")
            if args == ["git", "status", "--porcelain=v1", "-b"]:
                return _completed(args, 0, stdout="## main...origin/main [ahead 2]\n")
            if args[:2] == ["git", "add"]:
                return _completed(args, 0)
            if args == ["git", "diff", "--cached", "--quiet"]:
                return _completed(args, 0)
            if args[:2] == ["git", "push"]:
                return _completed(args, 0)
            raise AssertionError(f"Unexpected git command: {args}")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        monkeypatch.setattr(cli, "PROJECT_ROOT", repo_root)

        result = runner.invoke(cli.app, ["sync", "push"])

        assert result.exit_code == 0
        assert "Push completed" in result.stdout
        assert calls[0] == ["git", "diff", "--cached", "--name-only"]
        assert calls[1] == ["git", "status", "--porcelain=v1", "-b"]
        assert calls[2] == ["git", "add", "data/peel.db"]
        assert calls[3] == ["git", "diff", "--cached", "--quiet"]
        assert calls[4] == ["git", "push"]
