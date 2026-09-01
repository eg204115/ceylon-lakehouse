"""Open-Meteo daily weather extractor.

Contract: ``docs/contracts/open_meteo.yml``.

One request per location per run date. Nothing here parses the response body —
bytes land exactly as received. Request *building* is a pure function so it can
be asserted without a network, and the fetcher is injected so the whole extractor
runs offline in tests.

Per the contract, a location that fails every attempt is recorded as degraded and
does not fail the run (practice 63). A run in which every location fails is a
failure — that is not a flaky source, it is an outage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ceylon.config import Location, SourceConfig
from ceylon.fetch import Fetcher, FetchError
from ceylon.landing import LandedFile, landing_path, summarise_payload, write_atomic
from ceylon.observability import get_logger

__all__ = ["ExtractResult", "FetchSpec", "build_fetch_specs", "extract"]

_LOGGER = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class FetchSpec:
    """One planned request. Pure data, so planning is testable on its own."""

    location_id: str
    url: str
    params: dict[str, str]


@dataclass(frozen=True, slots=True)
class ExtractResult:
    """Outcome of one extraction run for this source."""

    source_id: str
    run_date: date
    landed: tuple[LandedFile, ...]
    degraded: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        """Whether every configured location landed."""
        return not self.degraded


def _build_params(
    location: Location, run_date: date, daily_variables: tuple[str, ...]
) -> dict[str, str]:
    return {
        "latitude": f"{location.latitude:.4f}",
        "longitude": f"{location.longitude:.4f}",
        # A single-date window: start and end are both the run date, so the
        # response has exactly one row and the request is a pure function of the
        # run date — which is what makes re-running a date reproducible.
        "start_date": run_date.isoformat(),
        "end_date": run_date.isoformat(),
        "daily": ",".join(daily_variables),
        "timezone": location.timezone,
    }


def build_fetch_specs(config: SourceConfig, run_date: date) -> tuple[FetchSpec, ...]:
    """Plan every request for one run date.

    Args:
        config: Validated source configuration.
        run_date: Date to collect.

    Returns:
        One spec per configured location, in configuration order.
    """
    return tuple(
        FetchSpec(
            location_id=location.id,
            url=config.endpoint,
            params=_build_params(location, run_date, config.daily_variables),
        )
        for location in config.locations
    )


def extract(
    config: SourceConfig,
    run_date: date,
    landing_root: Path,
    fetcher: Fetcher,
) -> ExtractResult:
    """Fetch every location for one run date and land the raw payloads.

    Args:
        config: Validated source configuration.
        run_date: Date to collect.
        landing_root: Root of the landing area.
        fetcher: Injected so tests run without a network.

    Returns:
        What landed and what degraded.

    Raises:
        FetchError: If every location failed. A total failure is an outage and
            must stop the run rather than land an empty date.
    """
    landed: list[LandedFile] = []
    degraded: list[str] = []

    for spec in build_fetch_specs(config, run_date):
        try:
            response = fetcher.get(spec.url, spec.params)
        except FetchError as exc:
            degraded.append(spec.location_id)
            _LOGGER.error(
                "extract.location_failed",
                extra={
                    "source": config.id,
                    "location": spec.location_id,
                    "run_date": run_date.isoformat(),
                    "reason": str(exc),
                },
            )
            continue

        destination = landing_path(landing_root, config.id, run_date, spec.location_id)
        write_atomic(destination, response.content)
        summary = summarise_payload(response.content)
        landed.append(
            LandedFile(name=spec.location_id, path=destination, summary=summary)
        )

        _LOGGER.info(
            "extract.location_landed",
            extra={
                "source": config.id,
                "location": spec.location_id,
                "run_date": run_date.isoformat(),
                "path": str(destination),
                "bytes": summary.bytes_written,
                "sha256": summary.sha256,
                "rows": summary.daily_row_count,
                "is_json": summary.is_json,
            },
        )

    if not landed:
        raise FetchError(
            f"{config.id}: every location failed for {run_date.isoformat()}"
        )

    return ExtractResult(
        source_id=config.id,
        run_date=run_date,
        landed=tuple(landed),
        degraded=tuple(degraded),
    )
