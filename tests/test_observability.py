"""Tests for structured logging."""

from __future__ import annotations

import json
import logging

import pytest

from ceylon.observability import JsonFormatter, configure_logging, get_logger


def record(**extra: object) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="ceylon.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="extract.landed",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(rec, key, value)
    return rec


class TestJsonFormatter:
    def test_emits_valid_single_line_json(self) -> None:
        line = JsonFormatter("run-1").format(record())
        assert "\n" not in line
        assert json.loads(line)["event"] == "extract.landed"

    def test_carries_the_fixed_fields(self) -> None:
        payload = json.loads(JsonFormatter("run-1").format(record()))
        assert payload.keys() >= {"timestamp", "level", "logger", "run_id", "event"}
        assert payload["run_id"] == "run-1"
        assert payload["level"] == "INFO"

    def test_promotes_extra_fields_to_top_level(self) -> None:
        payload = json.loads(
            JsonFormatter("run-1").format(record(source="open_meteo", rows=5))
        )
        assert payload["source"] == "open_meteo"
        assert payload["rows"] == 5

    @pytest.mark.parametrize(
        "field", ["password", "api_key", "auth_token", "client_secret", "CREDENTIAL"]
    )
    def test_redacts_anything_that_looks_like_a_credential(self, field: str) -> None:
        line = JsonFormatter("run-1").format(record(**{field: "hunter2"}))
        payload = json.loads(line)
        assert payload[field] == "***redacted***"
        assert "hunter2" not in json.dumps(payload)

    def test_records_the_error_type_on_an_exception(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            rec = record()
            rec.exc_info = sys.exc_info()
            payload = json.loads(JsonFormatter("run-1").format(rec))

        assert payload["error_type"] == "ValueError"
        assert "boom" in payload["traceback"]

    def test_serialises_values_json_cannot_represent(self) -> None:
        from pathlib import Path

        payload = json.loads(
            JsonFormatter("run-1").format(record(path=Path("a/b.json")))
        )
        assert "b.json" in payload["path"]


class TestConfigureLogging:
    def test_installs_exactly_one_handler(self) -> None:
        configure_logging("run-1")
        configure_logging("run-2")
        assert len(logging.getLogger().handlers) == 1

    def test_output_is_json_carrying_the_run_id(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging("run-abc")
        get_logger("ceylon.test").info("run.started", extra={"source": "open_meteo"})

        payload = json.loads(capsys.readouterr().err.strip())
        assert payload["run_id"] == "run-abc"
        assert payload["event"] == "run.started"
        assert payload["source"] == "open_meteo"
