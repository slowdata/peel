"""Shared test guards."""

from __future__ import annotations

import pytest

from peel import cli


@pytest.fixture(autouse=True)
def _no_real_spotify_playback(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """`peel ouvir` plays items in the running Spotify app; tests must never do that."""
    played: list[str | None] = []

    def record(uri_or_url: str | None) -> bool:
        played.append(uri_or_url)
        return False

    monkeypatch.setattr(cli, "_play_in_spotify", record)
    return played
