"""Opt-in pinned LanguageTool installation and short-lived loopback-only checks.

No cloud API, telemetry client, daemon, automatic download or new Python dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

import httpx

VERSION = "6.6"
ARCHIVE_URL = f"https://languagetool.org/download/LanguageTool-{VERSION}.zip"
ARCHIVE_SHA256 = "53600506b399bb5ffe1e4c8dec794fd378212f14aaf38ccef9b6f89314d11631"
MAX_DOWNLOAD_BYTES = 270_000_000
MAX_EXTRACT_BYTES = 600_000_000


class ProofreaderUnavailable(RuntimeError):
    """Keep original comments and continue the normal listening/sync flow."""


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return (
        (Path(base) if base and Path(base).is_absolute() else Path.home() / ".cache")
        / "peel"
        / "languagetool"
    )


def distribution() -> Path:
    return cache_root() / VERSION / f"LanguageTool-{VERSION}"


def available() -> bool:
    return bool(shutil.which("java") and (distribution() / "languagetool-server.jar").is_file())


def install(archive: Path | None = None) -> Path:
    """Explicit setup only. Verify the fixed artifact before bounded safe extraction."""
    target = cache_root() / VERSION
    jar = target / f"LanguageTool-{VERSION}" / "languagetool-server.jar"
    if jar.is_file():
        return jar.parent
    root = cache_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="install-", dir=root) as temporary:
        work = Path(temporary)
        if archive is None:
            archive = work / "download.zip"
            total = 0
            with httpx.stream("GET", ARCHIVE_URL, timeout=30, follow_redirects=False) as response:
                response.raise_for_status()
                with archive.open("xb") as handle:
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise ValueError("LanguageTool download exceeded the size limit")
                        handle.write(chunk)
        if archive.stat().st_size > MAX_DOWNLOAD_BYTES:
            raise ValueError("LanguageTool archive exceeded the size limit")
        digest = hashlib.sha256()
        with archive.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != ARCHIVE_SHA256:
            raise ValueError("LanguageTool archive checksum mismatch")
        extracted = work / "extracted"
        extracted.mkdir()
        with zipfile.ZipFile(archive) as zipped:
            entries = zipped.infolist()
            if sum(entry.file_size for entry in entries) > MAX_EXTRACT_BYTES:
                raise ValueError("LanguageTool extraction exceeded the size limit")
            for entry in entries:
                name = PurePosixPath(entry.filename)
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or not name.parts
                    or name.parts[0] != f"LanguageTool-{VERSION}"
                    or stat.S_ISLNK(entry.external_attr >> 16)
                    or "\\" in entry.filename
                ):
                    raise ValueError("Unsafe LanguageTool archive entry")
            zipped.extractall(extracted)
        if not (extracted / f"LanguageTool-{VERSION}" / "languagetool-server.jar").is_file():
            raise ValueError("LanguageTool server jar missing")
        (extracted / "installation.json").write_text(
            json.dumps({"version": VERSION, "archive_sha256": ARCHIVE_SHA256}) + "\n"
        )
        try:
            extracted.rename(target)
        except FileExistsError:
            if not jar.is_file():
                raise
    return jar.parent


class LocalLanguageTool:
    """Own just this Java child, check via 127.0.0.1, stop before displaying edits."""

    def __init__(self):
        self.process: subprocess.Popen | None = None
        self.client: httpx.Client | None = None
        self.temp: tempfile.TemporaryDirectory | None = None

    def __enter__(self) -> LocalLanguageTool:
        java = shutil.which("java")
        jar = distribution() / "languagetool-server.jar"
        if not java or not jar.is_file():
            raise ProofreaderUnavailable("Run uv run peel comments setup first")
        try:
            self.temp = tempfile.TemporaryDirectory(prefix="peel-languagetool-")
            config = Path(self.temp.name) / "server.properties"
            config.write_text("maxCheckTimeMillis=5000\nmaxTextLength=16000\n")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            self.process = subprocess.Popen(
                [
                    java,
                    "-Xms64m",
                    "-Xmx512m",
                    "-cp",
                    str(jar),
                    "org.languagetool.server.HTTPServer",
                    "--config",
                    str(config),
                    "--port",
                    str(port),
                ],
                cwd=jar.parent,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.client = httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                trust_env=False,
                follow_redirects=False,
                timeout=6,
            )
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise ProofreaderUnavailable("Local proofreader exited before startup")
                try:
                    response = self.client.get("/v2/languages", timeout=0.5)
                    if response.status_code == 200 and any(
                        language.get("longCode") == "pt-PT" for language in response.json()
                    ):
                        return self
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    pass
                time.sleep(0.15)
            raise ProofreaderUnavailable("Local proofreader startup timed out")
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def check(self, text: str) -> list[dict]:
        if self.client is None:
            raise ProofreaderUnavailable("Local proofreader is not running")
        try:
            response = self.client.post("/v2/check", data={"language": "pt-PT", "text": text})
            response.raise_for_status()
            if len(response.content) > 1_000_000:
                raise ProofreaderUnavailable("Local proofreader response exceeded limit")
            matches = response.json()["matches"]
            if not isinstance(matches, list) or not all(isinstance(m, dict) for m in matches):
                raise ValueError("Invalid matches")
            return matches
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise ProofreaderUnavailable("Local proofreader could not check this batch") from exc

    def __exit__(self, *_: object) -> None:
        client, self.client = self.client, None
        process, self.process = self.process, None
        temporary, self.temp = self.temp, None
        try:
            if client is not None:
                client.close()
        finally:
            try:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
            finally:
                if temporary is not None:
                    temporary.cleanup()
