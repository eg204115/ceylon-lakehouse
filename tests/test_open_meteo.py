"""Tests for the Open-Meteo extractor, including the idempotency guarantee."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ceylon.config import SourceConfig
from ceylon.fetch import FetchError
from ceylon.landing import landing_path
from ceylon.sources.open_meteo import build_fetch_specs, extract
from tests.conftest import StubFetcher, fixture_bytes

RUN_DATE = date(2026, 8, 1)


class TestBuildFetchSpecs:
    def test_one_request_per_location(self, source_config: SourceConfig) -> None:
        specs = build_fetch_specs(source_config, RUN_DATE)
        assert [spec.location_id for spec in specs] == ["colombo", "galle", "male"]

    def test_requests_a_single_date_window(self, source_config: SourceConfig) -> None:
        params = build_fetch_specs(source_config, RUN_DATE)[0].params
        assert params["start_date"] == "2026-08-01"
        assert params["end_date"] == params["start_date"]

    def test_carries_the_configured_variables(
        self, source_config: SourceConfig
    ) -> None:
        params = build_fetch_specs(source_config, RUN_DATE)[0].params
        assert params["daily"] == "temperature_2m_max,precipitation_sum"

    def test_uses_each_location_timezone(self, source_config: SourceConfig) -> None:
        specs = build_fetch_specs(source_config, RUN_DATE)
        assert specs[0].params["timezone"] == "Asia/Colombo"
        assert specs[2].params["timezone"] == "Indian/Maldives"

    def test_planning_is_pure(self, source_config: SourceConfig) -> None:
        # Same inputs, same plan — the property re-running a date depends on.
        assert build_fetch_specs(source_config, RUN_DATE) == build_fetch_specs(
            source_config, RUN_DATE
        )


class TestExtract:
    def test_lands_one_file_per_location(
        self, source_config: SourceConfig, stub_fetcher: StubFetcher, tmp_path: Path
    ) -> None:
        result = extract(source_config, RUN_DATE, tmp_path, stub_fetcher)
        assert len(result.landed) == 3
        assert result.is_complete
        for landed in result.landed:
            assert landed.path.is_file()

    def test_lands_bytes_verbatim(
        self, source_config: SourceConfig, stub_fetcher: StubFetcher, tmp_path: Path
    ) -> None:
        raw = fixture_bytes("open_meteo_colombo.json")
        stub_fetcher.payloads = {"colombo": raw}
        extract(source_config, RUN_DATE, tmp_path, stub_fetcher)
        landed = landing_path(tmp_path, "open_meteo", RUN_DATE, "colombo")
        assert landed.read_bytes() == raw

    def test_a_failing_location_degrades_rather_than_fails(
        self, source_config: SourceConfig, stub_fetcher: StubFetcher, tmp_path: Path
    ) -> None:
        # Practice 63: one flaky source must not take the platform down.
        stub_fetcher.failures = {"galle"}
        result = extract(source_config, RUN_DATE, tmp_path, stub_fetcher)
        assert result.degraded == ("galle",)
        assert len(result.landed) == 2
        assert not result.is_complete

    def test_total_failure_raises(
        self, source_config: SourceConfig, stub_fetcher: StubFetcher, tmp_path: Path
    ) -> None:
        # Every location down is an outage, not flakiness.
        stub_fetcher.failures = {"colombo", "galle", "male"}
        with pytest.raises(FetchError, match="every location failed"):
            extract(source_config, RUN_DATE, tmp_path, stub_fetcher)

    def test_lands_a_malformed_payload_without_complaining(
        self, source_config: SourceConfig, stub_fetcher: StubFetcher, tmp_path: Path
    ) -> None:
        # Bronze is unparsed and unjudged (practice 26). The summary records the
        # problem; the gate that rejects it arrives in Stage 3.
        truncated = fixture_bytes("open_meteo_truncated.json")
        stub_fetcher.payloads = {"colombo": truncated}
        result = extract(source_config, RUN_DATE, tmp_path, stub_fetcher)

        colombo = next(item for item in result.landed if item.name == "colombo")
        assert colombo.path.read_bytes() == truncated
        assert not colombo.summary.is_json

    def test_records_an_empty_response_as_empty(
        self, source_config: SourceConfig, stub_fetcher: StubFetcher, tmp_path: Path
    ) -> None:
        stub_fetcher.payloads = {"colombo": fixture_bytes("open_meteo_empty.json")}
        result = extract(source_config, RUN_DATE, tmp_path, stub_fetcher)
        colombo = next(item for item in result.landed if item.name == "colombo")
        assert colombo.summary.is_empty


class TestIdempotency:
    """The Stage 1 guarantee: re-running a date is safe and produces the same thing."""

    def test_rerunning_produces_byte_identical_output(
        self, source_config: SourceConfig, stub_fetcher: StubFetcher, tmp_path: Path
    ) -> None:
        extract(source_config, RUN_DATE, tmp_path, stub_fetcher)
        first = {
            path.name: path.read_bytes() for path in sorted(tmp_path.rglob("*.json"))
        }

        extract(source_config, RUN_DATE, tmp_path, stub_fetcher)
        second = {
            path.name: path.read_bytes() for path in sorted(tmp_path.rglob("*.json"))
        }

        assert first == second

    def test_rerunning_does_not_accumulate_files(
        self, source_config: SourceConfig, stub_fetcher: StubFetcher, tmp_path: Path
    ) -> None:
        extract(source_config, RUN_DATE, tmp_path, stub_fetcher)
        after_one = sorted(path.name for path in tmp_path.rglob("*.json"))

        for _ in range(3):
            extract(source_config, RUN_DATE, tmp_path, stub_fetcher)
        after_four = sorted(path.name for path in tmp_path.rglob("*.json"))

        assert after_one == after_four

    def test_a_rerun_overwrites_a_corrupted_payload(
        self, source_config: SourceConfig, stub_fetcher: StubFetcher, tmp_path: Path
    ) -> None:
        # This is the recovery path: bronze is repaired by re-running the date,
        # not by editing a file.
        extract(source_config, RUN_DATE, tmp_path, stub_fetcher)
        corrupted = landing_path(tmp_path, "open_meteo", RUN_DATE, "colombo")
        corrupted.write_bytes(b"garbage")

        extract(source_config, RUN_DATE, tmp_path, stub_fetcher)
        assert corrupted.read_bytes() != b"garbage"

    def test_separate_dates_coexist(
        self, source_config: SourceConfig, stub_fetcher: StubFetcher, tmp_path: Path
    ) -> None:
        extract(source_config, RUN_DATE, tmp_path, stub_fetcher)
        extract(source_config, date(2026, 8, 2), tmp_path, stub_fetcher)
        partitions = sorted(p.name for p in (tmp_path / "open_meteo").iterdir())
        assert partitions == ["run_date=2026-08-01", "run_date=2026-08-02"]
