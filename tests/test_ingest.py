"""End-to-end tests for the CLI, still entirely offline."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ceylon.ingest import main, run
from ceylon.runtime import new_run_id
from tests.conftest import StubFetcher

RUN_DATE = date(2026, 8, 1)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "sources.yml"
    path.write_text(
        """
version: 1
sources:
  open_meteo:
    enabled: true
    endpoint: https://example.invalid/forecast
    daily_variables: [temperature_2m_max]
    locations:
      - {id: colombo, name: Colombo, latitude: 6.9271, longitude: 79.8612,
         timezone: Asia/Colombo}
      - {id: galle, name: Galle, latitude: 6.0535, longitude: 80.2210,
         timezone: Asia/Colombo}
  disabled_source:
    enabled: false
    endpoint: https://example.invalid/other
    daily_variables: [x]
    locations:
      - {id: somewhere, name: Somewhere, latitude: 0.0, longitude: 0.0,
         timezone: UTC}
""",
        encoding="utf-8",
    )
    return path


class TestRun:
    def test_writes_a_manifest_beside_the_payloads(
        self, config_file: Path, stub_fetcher: StubFetcher, tmp_path: Path
    ) -> None:
        landing = tmp_path / "landing"
        run("open_meteo", RUN_DATE, config_file, landing, new_run_id(), stub_fetcher)

        manifest_path = (
            landing / "open_meteo" / "run_date=2026-08-01" / "_manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        assert manifest["status"] == "complete"
        assert manifest["landed_count"] == 2
        assert manifest["expected_count"] == 2
        assert manifest["degraded"] == []
        assert {entry["name"] for entry in manifest["files"]} == {"colombo", "galle"}
        assert all(len(entry["sha256"]) == 64 for entry in manifest["files"])

    def test_manifest_carries_the_run_id(
        self, config_file: Path, stub_fetcher: StubFetcher, tmp_path: Path
    ) -> None:
        run_id = new_run_id()
        landing = tmp_path / "landing"
        run("open_meteo", RUN_DATE, config_file, landing, run_id, stub_fetcher)

        manifest_path = (
            landing / "open_meteo" / "run_date=2026-08-01" / "_manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["run_id"] == run_id

    def test_manifest_records_degradation(
        self, config_file: Path, stub_fetcher: StubFetcher, tmp_path: Path
    ) -> None:
        stub_fetcher.failures = {"galle"}
        landing = tmp_path / "landing"
        run("open_meteo", RUN_DATE, config_file, landing, new_run_id(), stub_fetcher)

        manifest_path = (
            landing / "open_meteo" / "run_date=2026-08-01" / "_manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "degraded"
        assert manifest["degraded"] == ["galle"]
        assert manifest["landed_count"] == 1

    def test_refuses_a_disabled_source(
        self, config_file: Path, stub_fetcher: StubFetcher, tmp_path: Path
    ) -> None:
        with pytest.raises(Exception, match="disabled"):
            run(
                "disabled_source",
                RUN_DATE,
                config_file,
                tmp_path,
                new_run_id(),
                stub_fetcher,
            )

    def test_refuses_a_source_with_no_extractor(
        self, tmp_path: Path, stub_fetcher: StubFetcher
    ) -> None:
        path = tmp_path / "sources.yml"
        path.write_text(
            """
version: 1
sources:
  cse:
    enabled: true
    endpoint: https://example.invalid/cse
    daily_variables: [price]
    locations:
      - {id: colombo, name: Colombo, latitude: 6.9, longitude: 79.8,
         timezone: Asia/Colombo}
""",
            encoding="utf-8",
        )
        with pytest.raises(Exception, match="no extractor implemented"):
            run("cse", RUN_DATE, path, tmp_path, new_run_id(), stub_fetcher)


class TestMainExitCodes:
    """Exit codes are the scheduler's interface — worth asserting explicitly."""

    def test_rejects_a_malformed_run_date(self, config_file: Path) -> None:
        code = main(
            [
                "--source",
                "open_meteo",
                "--run-date",
                "01-08-2026",
                "--config",
                str(config_file),
            ]
        )
        assert code == 2

    def test_rejects_an_unknown_source(self, config_file: Path, tmp_path: Path) -> None:
        code = main(
            [
                "--source",
                "nope",
                "--run-date",
                "2026-08-01",
                "--config",
                str(config_file),
                "--landing-root",
                str(tmp_path),
            ]
        )
        assert code == 2

    def test_reports_a_missing_config_as_usage_error(self, tmp_path: Path) -> None:
        code = main(
            [
                "--source",
                "open_meteo",
                "--run-date",
                "2026-08-01",
                "--config",
                str(tmp_path / "absent.yml"),
            ]
        )
        assert code == 2

    def test_requires_both_arguments(self) -> None:
        with pytest.raises(SystemExit):
            main(["--source", "open_meteo"])


class TestIdempotencyThroughTheCli:
    def test_two_identical_runs_leave_identical_payloads(
        self, config_file: Path, stub_fetcher: StubFetcher, tmp_path: Path
    ) -> None:
        landing = tmp_path / "landing"
        args = ("open_meteo", RUN_DATE, config_file, landing)

        run(*args, new_run_id(), stub_fetcher)
        first = {
            path.name: path.read_bytes()
            for path in sorted(landing.rglob("*.json"))
            if path.name != "_manifest.json"
        }

        run(*args, new_run_id(), stub_fetcher)
        second = {
            path.name: path.read_bytes()
            for path in sorted(landing.rglob("*.json"))
            if path.name != "_manifest.json"
        }

        assert first == second

    def test_the_manifest_is_deliberately_not_identical(
        self, config_file: Path, stub_fetcher: StubFetcher, tmp_path: Path
    ) -> None:
        # Payloads are reproducible; provenance is not, and should not be. The
        # manifest records *who ran what when*, so a fresh run id is correct.
        landing = tmp_path / "landing"
        manifest_path = (
            landing / "open_meteo" / "run_date=2026-08-01" / "_manifest.json"
        )

        run("open_meteo", RUN_DATE, config_file, landing, new_run_id(), stub_fetcher)
        first = json.loads(manifest_path.read_text(encoding="utf-8"))

        run("open_meteo", RUN_DATE, config_file, landing, new_run_id(), stub_fetcher)
        second = json.loads(manifest_path.read_text(encoding="utf-8"))

        assert first["run_id"] != second["run_id"]
        assert first["files"] == second["files"]
