"""Ingestion entrypoint.

    python -m ceylon.ingest --source open_meteo --run-date 2026-08-01

Thin by design: parse arguments, wire dependencies, hand off. Every decision it
makes is delegated to a module that can be tested without it.

Alongside the payloads it writes ``_manifest.json``, recording what this run did.
That file is the seed of the ``run_metadata`` table in Stage 3 — the same fields,
written to disk before there is a table to write them to.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from ceylon.config import ConfigError, SourceConfig, load_sources
from ceylon.fetch import Fetcher, FetchError, HttpFetcher
from ceylon.landing import landing_dir, write_atomic
from ceylon.observability import configure_logging, get_logger
from ceylon.runtime import RunDateError, new_run_id, parse_run_date
from ceylon.sources import open_meteo

__all__ = ["main", "run"]

_LOGGER = get_logger(__name__)

_DEFAULT_CONFIG = Path("config/sources.yml")
_DEFAULT_LANDING_ROOT = Path("data/landing")
_MANIFEST_NAME = "_manifest.json"

# Exit codes are part of the interface: a scheduler distinguishes "the source is
# broken" from "you invoked it wrong", and only one of those is worth paging for.
_EXIT_OK = 0
_EXIT_USAGE = 2
_EXIT_FAILED = 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ceylon.ingest",
        description="Extract one source for one run date and land the raw payloads.",
    )
    parser.add_argument("--source", required=True, help="Source id, e.g. open_meteo")
    parser.add_argument(
        "--run-date",
        required=True,
        help="Date to collect, strict YYYY-MM-DD. Re-running a date is safe.",
    )
    parser.add_argument(
        "--config", type=Path, default=_DEFAULT_CONFIG, help="Path to sources.yml"
    )
    parser.add_argument(
        "--landing-root",
        type=Path,
        default=_DEFAULT_LANDING_ROOT,
        help="Root of the raw landing area",
    )
    return parser


def _write_manifest(
    landing_root: Path,
    config: SourceConfig,
    run_date: date,
    run_id: str,
    result: open_meteo.ExtractResult,
    started_at: datetime,
) -> Path:
    """Record what this run did, next to what it produced."""
    manifest = {
        "run_id": run_id,
        "source": config.id,
        "run_date": run_date.isoformat(),
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "duration_ms": int((datetime.now(UTC) - started_at).total_seconds() * 1000),
        "status": "complete" if result.is_complete else "degraded",
        "expected_count": len(config.locations),
        "landed_count": len(result.landed),
        "degraded": list(result.degraded),
        "files": [
            {
                "name": item.name,
                "path": item.path.name,
                "bytes": item.summary.bytes_written,
                "sha256": item.summary.sha256,
                "stable_sha256": item.summary.stable_sha256,
                "is_json": item.summary.is_json,
                "rows": item.summary.daily_row_count,
            }
            for item in result.landed
        ],
    }
    path = landing_dir(landing_root, config.id, run_date) / _MANIFEST_NAME
    write_atomic(path, json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"))
    return path


def run(
    source_id: str,
    run_date: date,
    config_path: Path,
    landing_root: Path,
    run_id: str,
    fetcher: Fetcher | None = None,
) -> open_meteo.ExtractResult:
    """Execute one extraction.

    Args:
        source_id: Which source to run.
        run_date: Date to collect.
        config_path: Path to ``sources.yml``.
        landing_root: Root of the landing area.
        run_id: Correlation id for this invocation.
        fetcher: Injected in tests; a real HTTP fetcher is built if omitted.

    Returns:
        What landed and what degraded.

    Raises:
        ConfigError: If configuration is missing, invalid, or the source is
            configured but disabled.
        FetchError: If every location failed.
    """
    started_at = datetime.now(UTC)
    config = load_sources(config_path).get(source_id)

    if not config.enabled:
        raise ConfigError(f"source {source_id!r} is disabled in {config_path}")
    if source_id != "open_meteo":
        raise ConfigError(
            f"no extractor implemented for {source_id!r}; only 'open_meteo' so far"
        )

    _LOGGER.info(
        "run.started",
        extra={
            "source": config.id,
            "run_date": run_date.isoformat(),
            "locations": len(config.locations),
            "landing_root": str(landing_root),
        },
    )

    active_fetcher = (
        fetcher
        if fetcher is not None
        else HttpFetcher(
            timeout_seconds=config.timeout_seconds,
            max_attempts=config.max_attempts,
            backoff_seconds=config.backoff_seconds,
        )
    )

    result = open_meteo.extract(config, run_date, landing_root, active_fetcher)
    manifest_path = _write_manifest(
        landing_root, config, run_date, run_id, result, started_at
    )

    _LOGGER.info(
        "run.finished",
        extra={
            "source": config.id,
            "run_date": run_date.isoformat(),
            "status": "complete" if result.is_complete else "degraded",
            "landed": len(result.landed),
            "degraded": list(result.degraded),
            "manifest": str(manifest_path),
            "duration_ms": int((datetime.now(UTC) - started_at).total_seconds() * 1000),
        },
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint.

    Args:
        argv: Arguments excluding the program name. Defaults to ``sys.argv``.

    Returns:
        ``0`` on success (including a degraded run), ``1`` on failure, ``2`` on
        a usage or configuration error.
    """
    args = _build_parser().parse_args(argv)
    run_id = new_run_id()
    configure_logging(run_id)

    try:
        run_date = parse_run_date(args.run_date)
    except RunDateError as exc:
        _LOGGER.error("run.bad_arguments", extra={"reason": str(exc)})
        return _EXIT_USAGE

    try:
        result = run(
            source_id=args.source,
            run_date=run_date,
            config_path=args.config,
            landing_root=args.landing_root,
            run_id=run_id,
        )
    except ConfigError as exc:
        _LOGGER.error("run.bad_configuration", extra={"reason": str(exc)})
        return _EXIT_USAGE
    except FetchError as exc:
        _LOGGER.error("run.failed", extra={"reason": str(exc)})
        return _EXIT_FAILED

    # A degraded run still succeeds: one broken location must not fail the
    # platform (practice 63). It is visible in the manifest and in the logs, and
    # from Stage 3 it will raise an alert.
    return _EXIT_OK if result.landed else _EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
