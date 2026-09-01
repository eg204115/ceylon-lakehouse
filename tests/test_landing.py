"""Tests for raw landing: deterministic paths, atomic writes, payload summaries."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ceylon.landing import (
    landing_dir,
    landing_path,
    summarise_payload,
    write_atomic,
)
from tests.conftest import fixture_bytes

RUN_DATE = date(2026, 8, 1)


class TestPaths:
    def test_path_is_hive_partitioned_by_run_date(self, tmp_path: Path) -> None:
        path = landing_path(tmp_path, "open_meteo", RUN_DATE, "colombo")
        assert path == tmp_path / "open_meteo" / "run_date=2026-08-01" / "colombo.json"

    def test_path_is_a_pure_function_of_its_inputs(self, tmp_path: Path) -> None:
        first = landing_path(tmp_path, "open_meteo", RUN_DATE, "colombo")
        second = landing_path(tmp_path, "open_meteo", RUN_DATE, "colombo")
        assert first == second

    def test_different_dates_do_not_collide(self, tmp_path: Path) -> None:
        first = landing_path(tmp_path, "open_meteo", RUN_DATE, "colombo")
        second = landing_path(tmp_path, "open_meteo", date(2026, 8, 2), "colombo")
        assert first != second

    def test_dir_matches_path_parent(self, tmp_path: Path) -> None:
        directory = landing_dir(tmp_path, "open_meteo", RUN_DATE)
        path = landing_path(tmp_path, "open_meteo", RUN_DATE, "colombo")
        assert path.parent == directory


class TestWriteAtomic:
    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        destination = tmp_path / "deep" / "nested" / "payload.json"
        write_atomic(destination, b"content")
        assert destination.read_bytes() == b"content"

    def test_writes_bytes_verbatim(self, tmp_path: Path) -> None:
        # Bronze stores what arrived. No re-encoding, no pretty-printing.
        raw = fixture_bytes("open_meteo_colombo.json")
        destination = tmp_path / "payload.json"
        write_atomic(destination, raw)
        assert destination.read_bytes() == raw

    def test_rewriting_replaces_rather_than_appends(self, tmp_path: Path) -> None:
        destination = tmp_path / "payload.json"
        write_atomic(destination, b"first")
        write_atomic(destination, b"second")
        assert destination.read_bytes() == b"second"

    def test_leaves_no_temporary_files_behind(self, tmp_path: Path) -> None:
        write_atomic(tmp_path / "payload.json", b"content")
        assert [p.name for p in tmp_path.iterdir()] == ["payload.json"]

    def test_a_failed_write_leaves_the_previous_version_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        destination = tmp_path / "payload.json"
        write_atomic(destination, b"original")

        def explode(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(Path, "replace", explode)
        with pytest.raises(OSError, match="disk full"):
            write_atomic(destination, b"replacement")

        assert destination.read_bytes() == b"original"
        assert [p.name for p in tmp_path.iterdir()] == ["payload.json"]


class TestSummarisePayload:
    def test_summarises_a_good_payload(self) -> None:
        summary = summarise_payload(fixture_bytes("open_meteo_colombo.json"))
        assert summary.is_json
        assert summary.daily_row_count == 1
        assert not summary.is_empty
        assert len(summary.sha256) == 64

    def test_detects_an_empty_response(self) -> None:
        # HTTP 200, well-formed, and carrying nothing. The silent failure.
        summary = summarise_payload(fixture_bytes("open_meteo_empty.json"))
        assert summary.is_json
        assert summary.daily_row_count == 0
        assert summary.is_empty

    def test_survives_a_truncated_payload(self) -> None:
        summary = summarise_payload(fixture_bytes("open_meteo_truncated.json"))
        assert not summary.is_json
        assert summary.daily_row_count is None
        assert summary.bytes_written > 0

    def test_survives_an_html_error_page(self) -> None:
        summary = summarise_payload(fixture_bytes("open_meteo_html_error.json"))
        assert not summary.is_json
        assert summary.daily_row_count is None

    def test_survives_undecodable_bytes(self) -> None:
        summary = summarise_payload(b"\xff\xfe\x00\x01")
        assert not summary.is_json

    def test_handles_json_that_is_not_an_object(self) -> None:
        assert summarise_payload(b"[1, 2, 3]").daily_row_count is None

    def test_handles_daily_without_a_time_array(self) -> None:
        assert summarise_payload(b'{"daily": {}}').daily_row_count is None

    def test_sha256_is_stable_and_content_addressed(self) -> None:
        raw = fixture_bytes("open_meteo_colombo.json")
        assert summarise_payload(raw).sha256 == summarise_payload(raw).sha256
        assert summarise_payload(raw).sha256 != summarise_payload(raw + b" ").sha256

    def test_row_count_scales_with_the_response(self) -> None:
        many = json.dumps({"daily": {"time": ["2026-08-01", "2026-08-02"]}}).encode()
        assert summarise_payload(many).daily_row_count == 2


class TestStableDigest:
    """A live source is not byte-deterministic; its data still is.

    Open-Meteo stamps ``generationtime_ms`` — a server-side performance counter —
    into every response, so two fetches of the same date differ in bytes while
    carrying identical weather. ``stable_sha256`` is what distinguishes a real
    upstream revision from that noise.
    """

    def test_volatile_fields_do_not_change_the_stable_digest(self) -> None:
        first = b'{"generationtime_ms": 0.05, "daily": {"time": ["2026-08-20"]}}'
        second = b'{"generationtime_ms": 9.91, "daily": {"time": ["2026-08-20"]}}'

        assert summarise_payload(first).sha256 != summarise_payload(second).sha256
        assert (
            summarise_payload(first).stable_sha256
            == summarise_payload(second).stable_sha256
        )

    def test_a_real_data_change_does_change_the_stable_digest(self) -> None:
        before = b'{"generationtime_ms": 0.05, "daily": {"temperature_2m_max": [28.8]}}'
        after = b'{"generationtime_ms": 0.05, "daily": {"temperature_2m_max": [31.2]}}'
        assert (
            summarise_payload(before).stable_sha256
            != summarise_payload(after).stable_sha256
        )

    def test_key_order_does_not_change_the_stable_digest(self) -> None:
        # Canonicalised, so a provider reordering its JSON is not a revision.
        one = b'{"latitude": 6.9, "longitude": 79.8}'
        other = b'{"longitude": 79.8, "latitude": 6.9}'
        assert (
            summarise_payload(one).stable_sha256
            == summarise_payload(other).stable_sha256
        )

    def test_is_absent_when_the_payload_is_not_json(self) -> None:
        assert summarise_payload(b"<html>error</html>").stable_sha256 is None
