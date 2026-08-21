from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click import unstyle
from typer.testing import CliRunner

import peel.cli as cli
from peel.db import DB

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


class TestAutomaticStateSync:
    def test_canonical_interactive_command_checks_remote_state(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        calls: list[tuple[Path, Path]] = []
        monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(cli.settings, "db_path", "data/peel.db")
        monkeypatch.setattr(cli, "_OFFLINE_MODE", False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.setattr(
            cli,
            "sync_remote_state",
            lambda db_path, root: (
                calls.append((db_path, root))
                or SimpleNamespace(status="current", local_week="2026-W32")
            ),
        )

        cli._auto_sync_state("feedback")

        assert calls == [(tmp_path / "data" / "peel.db", tmp_path)]

    def test_offline_mode_skips_remote_state(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(cli.settings, "db_path", "data/peel.db")
        monkeypatch.setattr(cli, "_OFFLINE_MODE", True)

        def remote(*_: object) -> None:
            raise AssertionError("network used")

        monkeypatch.setattr(cli, "sync_remote_state", remote)

        cli._auto_sync_state("feedback")


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
    def test_sync_pull_updates_state_without_git_pull(self, monkeypatch, tmp_path: Path) -> None:
        calls: list[tuple[Path, Path]] = []

        def fake_sync(db_path: Path, project_root: Path):
            calls.append((db_path, project_root))
            return SimpleNamespace(
                status="updated",
                previous_week="2026-W31",
                local_week="2026-W32",
                backup_path=tmp_path / "backup.db",
            )

        monkeypatch.setattr(cli, "sync_remote_state", fake_sync)
        monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)

        result = runner.invoke(cli.app, ["sync", "pull"])

        assert result.exit_code == 0
        output = unstyle(result.stdout)
        assert "2026-W31" in output
        assert "2026-W32" in output
        assert calls == [(tmp_path / "data" / "peel.db", tmp_path)]


class TestStateReports:
    def test_push_regenerates_only_latest_report_and_preserves_history(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "data" / "peel.db"
        db_path.parent.mkdir(parents=True)
        reports_dir = tmp_path / "data" / "reports"
        reports_dir.mkdir(parents=True)
        historical_path = reports_dir / "2026-W31.md"
        historical_path.write_text("frozen historical report\n", encoding="utf-8")

        db = DB(str(db_path))
        db.init_schema()
        for week in ("2026-W31", "2026-W32"):
            db.conn.execute(
                """
                INSERT INTO tracks
                (spotify_uri, source_id, artist, title, source_url, added_at, added_at_week)
                VALUES (?, 'source-a', 'Artist', 'Track', NULL, ?, ?)
                """,
                (f"spotify:track:{week}", "2026-08-08T10:00:00+00:00", week),
            )
            db.upsert_feedback(f"spotify:track:{week}", "like", None)
            db.replace_album_queue(week, [])
        db.close()
        monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)

        paths = cli._regenerate_state_reports(db_path)

        assert [path.name for path in paths] == ["2026-W32.md"]
        assert historical_path.read_text(encoding="utf-8") == "frozen historical report\n"


class TestTemporaryStatePush:
    def test_push_state_checkout_commits_on_latest_remote_main(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        seed = tmp_path / "seed"
        seed.mkdir()

        def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
            )

        git(seed, "init", "-b", "main")
        git(seed, "config", "user.name", "Test")
        git(seed, "config", "user.email", "test@example.com")
        (seed / "data" / "reports").mkdir(parents=True)
        (seed / "data" / "peel.db").write_text("old-db", encoding="utf-8")
        (seed / "data" / "reports" / "old.md").write_text("old", encoding="utf-8")
        git(seed, "add", ".")
        git(seed, "commit", "-m", "initial")
        bare = tmp_path / "remote.git"
        subprocess.run(
            ["git", "clone", "--bare", str(seed), str(bare)],
            check=True,
            capture_output=True,
            text=True,
        )
        local = tmp_path / "local"
        subprocess.run(
            ["git", "clone", str(bare), str(local)],
            check=True,
            capture_output=True,
            text=True,
        )
        (local / "data" / "peel.db").write_text("new-db", encoding="utf-8")
        (local / "data" / "reports" / "new.md").write_text("new", encoding="utf-8")
        monkeypatch.setattr(cli, "PROJECT_ROOT", local)
        monkeypatch.setattr(cli, "STATE_CLONE_URL", str(bare))
        monkeypatch.setattr(cli, "_assert_state_push_target", lambda _: None)

        changed = cli._push_state_checkout(
            local / "data" / "peel.db",
            [local / "data" / "reports" / "new.md"],
            expected_remote_blob_sha=cli.git_blob_sha(seed / "data" / "peel.db"),
        )

        assert changed is True
        assert git(bare, "show", "main:data/peel.db").stdout == "new-db"
        assert git(bare, "show", "main:data/reports/new.md").stdout == "new"

    def test_push_target_must_match_canonical_repository(self) -> None:
        with pytest.raises(cli.StateSyncError, match="não corresponde"):
            cli._assert_state_push_target("git@github.com:someone/fork.git")

        cli._assert_state_push_target("git@github.com:slowdata/peel.git")
        cli._assert_state_push_target("https://github.com/slowdata/peel.git")

    def test_push_state_checkout_detects_remote_race(self, monkeypatch, tmp_path: Path) -> None:
        db_path = tmp_path / "local.db"
        db_path.write_text("local", encoding="utf-8")
        monkeypatch.setattr(
            cli,
            "_run_git",
            lambda args: _completed(args, 0, stdout="git@example.test:repo.git\n"),
        )
        monkeypatch.setattr(cli, "_assert_state_push_target", lambda _: None)

        def fake_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
            if args[0] == "clone":
                checkout = Path(args[-1])
                (checkout / "data").mkdir(parents=True)
                (checkout / "data" / "peel.db").write_text("new remote", encoding="utf-8")
                return _completed(args, 0)
            raise AssertionError(args)

        monkeypatch.setattr(cli, "_run_git_in", fake_git)

        with pytest.raises(cli.StateSyncError, match="estado remoto avançou"):
            cli._push_state_checkout(
                db_path,
                [],
                expected_remote_blob_sha="previous-remote-sha",
            )


class TestSyncPush:
    def test_sync_push_uses_latest_remote_checkout_not_local_branch(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "data" / "peel.db"
        db_path.parent.mkdir(parents=True)
        db_path.write_text("db", encoding="utf-8")
        calls: list[tuple[str, Path]] = []
        monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(
            cli,
            "sync_remote_state",
            lambda path, root: (
                calls.append(("pull", path)) or SimpleNamespace(remote_blob_sha="remote-sha")
            ),
        )
        monkeypatch.setattr(cli, "_regenerate_state_reports", lambda _: [])
        monkeypatch.setattr(
            cli,
            "_push_state_checkout",
            lambda path, reports, expected_remote_blob_sha: calls.append(("push", path)) or True,
        )
        monkeypatch.setattr(
            cli,
            "mark_local_state_synced",
            lambda path: calls.append(("mark", path)),
        )

        result = runner.invoke(cli.app, ["sync", "push"])

        assert result.exit_code == 0
        assert "Push completed" in result.stdout
        assert calls == [
            ("pull", db_path),
            ("push", db_path),
            ("mark", db_path),
        ]

    def test_sync_push_rebases_feedback_when_weekly_advanced(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "data" / "peel.db"
        db_path.parent.mkdir(parents=True)
        db_path.write_text("db", encoding="utf-8")
        monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)

        def conflict(*_: object) -> None:
            raise cli.LocalStateConflict("both changed")

        calls: list[str] = []
        monkeypatch.setattr(cli, "sync_remote_state", conflict)
        monkeypatch.setattr(
            cli,
            "merge_remote_state_for_push",
            lambda path: (
                calls.append("merge") or SimpleNamespace(remote_blob_sha="merged-remote-sha")
            ),
        )
        monkeypatch.setattr(cli, "_regenerate_state_reports", lambda _: [])
        monkeypatch.setattr(
            cli,
            "_push_state_checkout",
            lambda path, reports, expected_remote_blob_sha: (
                calls.append(f"push:{expected_remote_blob_sha}") or True
            ),
        )
        monkeypatch.setattr(cli, "mark_local_state_synced", lambda path: calls.append("mark"))

        result = runner.invoke(cli.app, ["sync", "push"])

        assert result.exit_code == 0
        assert "integrado" in result.stdout
        assert calls == ["merge", "push:merged-remote-sha", "mark"]

    def test_sync_push_stops_on_local_remote_conflict(self, monkeypatch, tmp_path: Path) -> None:
        db_path = tmp_path / "data" / "peel.db"
        db_path.parent.mkdir(parents=True)
        db_path.write_text("db", encoding="utf-8")
        monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)

        def conflict(*_: object) -> None:
            raise cli.StateSyncError("local and remote changed")

        monkeypatch.setattr(cli, "sync_remote_state", conflict)
        pushed = False

        def push(*_: object) -> bool:
            nonlocal pushed
            pushed = True
            return True

        monkeypatch.setattr(cli, "_push_state_checkout", push)

        result = runner.invoke(cli.app, ["sync", "push"])

        assert result.exit_code == 1
        assert "local and remote changed" in result.stdout
        assert pushed is False
