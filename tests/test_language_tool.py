from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

import peel.language_tool as lt


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def archive(tmp_path, monkeypatch, name=None, mode=0, body=b"server"):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zipped:
        info = zipfile.ZipInfo(name or f"LanguageTool-{lt.VERSION}/languagetool-server.jar")
        info.external_attr = mode << 16
        zipped.writestr(info, body)
    path = tmp_path / "engine.zip"
    path.write_bytes(data.getvalue())
    monkeypatch.setattr(lt, "ARCHIVE_SHA256", hashlib.sha256(data.getvalue()).hexdigest())
    return path


def test_explicit_local_install_is_checksum_pinned_atomic_and_idempotent(tmp_path, monkeypatch):
    path = archive(tmp_path, monkeypatch)
    installed = lt.install(path)
    assert (installed / "languagetool-server.jar").read_bytes() == b"server"
    assert (installed.parent / "installation.json").is_file()
    assert lt.install(path) == installed
    assert not list(lt.cache_root().glob("install-*"))


def test_bad_checksum_never_extracts_or_overwrites_existing_files(tmp_path):
    path = tmp_path / "bad.zip"
    path.write_bytes(b"not the pinned artifact")
    with pytest.raises(ValueError, match="checksum"):
        lt.install(path)
    assert not lt.distribution().exists()


@pytest.mark.parametrize(
    "name,mode",
    [
        ("../escape", 0),
        ("/absolute", 0),
        ("unrelated/server.jar", 0),
        (f"LanguageTool-{lt.VERSION}/../escape", 0),
        (f"LanguageTool-{lt.VERSION}/link", 0o120777),
        (f"LanguageTool-{lt.VERSION}/bad\\path", 0),
    ],
)
def test_archive_path_and_symlink_protections(tmp_path, monkeypatch, name, mode):
    path = archive(tmp_path, monkeypatch, name, mode)
    with pytest.raises(ValueError, match="Unsafe"):
        lt.install(path)
    assert not lt.distribution().exists()
    assert not (tmp_path / "escape").exists()


def test_archive_expansion_limit(tmp_path, monkeypatch):
    path = archive(tmp_path, monkeypatch)
    monkeypatch.setattr(lt, "MAX_EXTRACT_BYTES", 1)
    with pytest.raises(ValueError, match="extraction"):
        lt.install(path)


def test_missing_java_or_archive_never_downloads_or_starts_process(monkeypatch):
    monkeypatch.setattr(lt.shutil, "which", lambda *a: None)
    popen = MagicMock()
    monkeypatch.setattr(lt.subprocess, "Popen", popen)
    with pytest.raises(lt.ProofreaderUnavailable), lt.LocalLanguageTool():
        pass
    popen.assert_not_called()


def fake_engine(monkeypatch, handler):
    folder = lt.distribution()
    folder.mkdir(parents=True)
    (folder / "languagetool-server.jar").write_bytes(b"synthetic")
    monkeypatch.setattr(lt.shutil, "which", lambda *a: "/fake/java")
    process = MagicMock()
    process.poll.return_value = None
    popen = MagicMock(return_value=process)
    monkeypatch.setattr(lt.subprocess, "Popen", popen)
    actual_client = httpx.Client
    clients = []

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["base_url"].startswith("http://127.0.0.1:")
        instance = actual_client(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(instance)
        return instance

    monkeypatch.setattr(lt.httpx, "Client", client)
    return process, popen, clients


def test_only_loopback_portuguese_requests_and_child_is_always_stopped(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.host == "127.0.0.1"
        if request.url.path.endswith("languages"):
            return httpx.Response(200, json=[{"longCode": "pt-PT"}])
        assert b"language=pt-PT" in request.content
        assert b"text=" in request.content
        return httpx.Response(200, json={"matches": []})

    process, popen, clients = fake_engine(monkeypatch, handler)
    with lt.LocalLanguageTool() as engine:
        assert engine.check("Texto privado, só local.") == []
        temporary = Path(engine.temp.name)
    assert process.terminate.call_count == 1
    assert process.wait.call_count == 1
    assert clients[0].is_closed and not temporary.exists()
    assert len(seen) == 2
    args = popen.call_args.args[0]
    assert "--public" not in args and "--allow-origin" not in args
    assert "shell" not in popen.call_args.kwargs


def test_http_close_failure_still_stops_java_and_removes_temporary_config(monkeypatch):
    process, _, _ = fake_engine(
        monkeypatch, lambda req: httpx.Response(200, json=[{"longCode": "pt-PT"}])
    )
    engine = lt.LocalLanguageTool().__enter__()
    temporary = Path(engine.temp.name)
    engine.client.close()
    engine.client.close = MagicMock(side_effect=OSError)
    with pytest.raises(OSError):
        engine.__exit__(None, None, None)
    process.terminate.assert_called_once()
    assert not temporary.exists() and engine.process is None


def test_crashed_startup_does_not_leave_client_or_temp_files(monkeypatch):
    process, _, clients = fake_engine(monkeypatch, lambda req: httpx.Response(500))
    process.poll.return_value = 1
    engine = lt.LocalLanguageTool()
    with pytest.raises(lt.ProofreaderUnavailable):
        engine.__enter__()
    assert clients[0].is_closed and engine.temp is None


def test_startup_deadline_and_terminate_timeout_only_kill_owned_child(monkeypatch):
    process, _, clients = fake_engine(monkeypatch, lambda req: httpx.Response(500))
    monkeypatch.setattr(lt.time, "monotonic", MagicMock(side_effect=[0, 21]))
    process.wait.side_effect = [lt.subprocess.TimeoutExpired("fake-java", 3), None]
    with pytest.raises(lt.ProofreaderUnavailable), lt.LocalLanguageTool():
        pass
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert clients[0].is_closed


@pytest.mark.parametrize("payload", [{}, {"matches": "bad"}, {"matches": ["bad"]}])
def test_malformed_engine_response_is_not_a_correction(monkeypatch, payload):
    def handler(request):
        return httpx.Response(
            200, json=[{"longCode": "pt-PT"}] if request.url.path.endswith("languages") else payload
        )

    process, _, _ = fake_engine(monkeypatch, handler)
    with pytest.raises(lt.ProofreaderUnavailable), lt.LocalLanguageTool() as engine:
        engine.check("Texto")
    process.terminate.assert_called_once()


def test_redirects_never_send_comments_outside_loopback(monkeypatch):
    def handler(request):
        if request.url.path.endswith("languages"):
            return httpx.Response(200, json=[{"longCode": "pt-PT"}])
        return httpx.Response(302, headers={"Location": "https://outside.invalid/v2/check"})

    process, _, _ = fake_engine(monkeypatch, handler)
    with pytest.raises(lt.ProofreaderUnavailable), lt.LocalLanguageTool() as engine:
        engine.check("Private")
    process.terminate.assert_called_once()
