from __future__ import annotations

import json
from pathlib import Path

import httpx
from typer.testing import CliRunner

import peel.cli as cli
from peel.doctor_sources import (
    SourceDoctorResult,
    SourceDoctorSpec,
    inspect_registered_sources,
    load_source_registry,
)

runner = CliRunner()


class TestLoadSourceRegistry:
    def test_load_source_registry_parses_tiers(self, tmp_path: Path) -> None:
        registry_path = tmp_path / "registry.json"
        registry_path.write_text(
            json.dumps(
                {
                    "_meta": {"version": "1.0"},
                    "tier_1": [
                        {
                            "name": "Guardian Music",
                            "id": "guardian-music",
                            "type": "rss",
                            "feed_url": "https://www.theguardian.com/music/rss",
                        }
                    ],
                    "misc": ["ignored"],
                }
            ),
            encoding="utf-8",
        )

        specs = load_source_registry(registry_path)

        assert specs == [
            SourceDoctorSpec(
                source_id="guardian-music",
                name="Guardian Music",
                type="rss",
                url="https://www.theguardian.com/music/rss",
            )
        ]


class TestInspectRegisteredSources:
    def test_inspect_registered_sources_handles_rss_json_and_scrape(
        self,
        monkeypatch,
    ) -> None:
        rss_bytes = (
            Path(__file__).resolve().parent / "fixtures" / "pitchfork_best_albums.xml"
        ).read_bytes()

        def fake_get(
            url: str,
            headers: dict[str, str],
            follow_redirects: bool,
            timeout: float,
        ) -> httpx.Response:
            if url == "https://example.test/rss":
                return httpx.Response(
                    200,
                    content=rss_bytes,
                    headers={"content-type": "application/rss+xml"},
                )
            if url == "https://example.test/json":
                return httpx.Response(
                    200,
                    json={"results": [1, 2, 3]},
                    headers={"content-type": "application/json"},
                )
            if url == "https://example.test/scrape":
                return httpx.Response(
                    200,
                    content=b"<html><body>ok</body></html>",
                    headers={"content-type": "text/html"},
                )
            if url == "https://example.test/dead":
                return httpx.Response(
                    404,
                    content=b"not found",
                    headers={"content-type": "text/html"},
                )
            raise AssertionError(f"Unexpected URL: {url}")

        monkeypatch.setattr("peel.doctor_sources.httpx.get", fake_get)

        results = inspect_registered_sources(
            [
                SourceDoctorSpec(
                    source_id="rss-source",
                    name="RSS Source",
                    type="rss",
                    url="https://example.test/rss",
                ),
                SourceDoctorSpec(
                    source_id="json-source",
                    name="JSON Source",
                    type="api_json",
                    url="https://example.test/json",
                ),
                SourceDoctorSpec(
                    source_id="scrape-source",
                    name="Scrape Source",
                    type="scrape",
                    url="https://example.test/scrape",
                ),
                SourceDoctorSpec(
                    source_id="dead-source",
                    name="Dead Source",
                    type="rss",
                    url="https://example.test/dead",
                ),
            ],
        )

        assert results[0].ok is True
        assert results[0].entries > 0
        assert results[0].http_status == 200

        assert results[1].ok is True
        assert results[1].entries == 3
        assert results[1].http_status == 200

        assert results[2].ok is True
        assert results[2].entries == 0
        assert results[2].note == "scrape target"

        assert results[3].ok is False
        assert results[3].http_status == 404
        assert results[3].note == "HTTP 404"


class TestDoctorSourcesCommand:
    def test_doctor_sources_command_renders_table_and_json(self, monkeypatch) -> None:
        results = [
            SourceDoctorResult(
                source_id="guardian-music",
                name="Guardian Music",
                type="rss",
                url="https://www.theguardian.com/music/rss",
                http_status=200,
                entries=12,
                ok=True,
                note="",
            )
        ]
        monkeypatch.setattr(cli, "inspect_registered_sources", lambda: results)

        table_result = runner.invoke(cli.app, ["doctor", "sources"])
        assert table_result.exit_code == 0
        assert "Doctor sources" in table_result.stdout
        assert "Guardian Music" in table_result.stdout
        assert "200" in table_result.stdout

        json_result = runner.invoke(cli.app, ["doctor", "sources", "--json"])
        assert json_result.exit_code == 0
        payload = json.loads(json_result.stdout)
        assert payload == [
            {
                "source_id": "guardian-music",
                "name": "Guardian Music",
                "type": "rss",
                "url": "https://www.theguardian.com/music/rss",
                "http_status": 200,
                "entries": 12,
                "ok": True,
                "note": "",
            }
        ]
