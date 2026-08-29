"""Tests for run identity and run-date handling."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from ceylon.runtime import RunDateError, new_run_id, parse_run_date


class TestParseRunDate:
    def test_parses_a_valid_date(self) -> None:
        assert parse_run_date("2026-08-01") == date(2026, 8, 1)

    @pytest.mark.parametrize(
        "value",
        [
            "20260801",  # compact form — ambiguous, rejected on purpose
            "2026-8-1",  # unpadded
            "2026-08-01T00:00:00",  # timestamp, accepted by fromisoformat on 3.11+
            "01-08-2026",  # day first
            "2026/08/01",
            "yesterday",
            "",
            " 2026-08-01",
        ],
    )
    def test_rejects_anything_that_is_not_strict_iso(self, value: str) -> None:
        with pytest.raises(RunDateError, match="must be YYYY-MM-DD"):
            parse_run_date(value)

    def test_rejects_a_well_formed_but_impossible_date(self) -> None:
        with pytest.raises(RunDateError, match="not a real date"):
            parse_run_date("2026-02-30")

    def test_accepts_a_leap_day(self) -> None:
        assert parse_run_date("2028-02-29") == date(2028, 2, 29)


class TestNewRunId:
    def test_embeds_the_given_timestamp(self) -> None:
        moment = datetime(2026, 8, 1, 13, 45, 0, tzinfo=UTC)
        assert new_run_id(moment).startswith("20260801T134500Z-")

    def test_has_a_stable_shape(self) -> None:
        run_id = new_run_id(datetime(2026, 8, 1, 13, 45, 0, tzinfo=UTC))
        stamp, _, suffix = run_id.partition("-")
        assert len(stamp) == 16
        assert len(suffix) == 8
        assert suffix.isalnum()

    def test_two_ids_in_the_same_second_are_distinct(self) -> None:
        moment = datetime(2026, 8, 1, 13, 45, 0, tzinfo=UTC)
        assert new_run_id(moment) != new_run_id(moment)

    def test_ids_sort_chronologically(self) -> None:
        earlier = new_run_id(datetime(2026, 8, 1, 13, 45, 0, tzinfo=UTC))
        later = new_run_id(datetime(2026, 8, 1, 13, 45, 1, tzinfo=UTC))
        assert earlier < later

    def test_normalises_a_non_utc_timestamp_to_utc(self) -> None:
        colombo = timezone(timedelta(hours=5, minutes=30))
        moment = datetime(2026, 8, 1, 19, 15, 0, tzinfo=colombo)
        assert new_run_id(moment).startswith("20260801T134500Z-")

    def test_rejects_a_naive_timestamp(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            new_run_id(datetime(2026, 8, 1, 13, 45, 0))  # noqa: DTZ001

    def test_defaults_to_now(self) -> None:
        before = datetime.now(UTC).strftime("%Y%m%d")
        assert new_run_id().startswith(before)
