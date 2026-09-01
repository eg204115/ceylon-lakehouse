"""Shared fixtures.

Nothing in the suite touches the network. ``StubFetcher`` stands in for the real
one, so the entire extractor runs offline, deterministically, in milliseconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ceylon.config import Location, SourceConfig
from ceylon.fetch import FetchError, Response

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    """Read a committed fixture as raw bytes."""
    return (FIXTURES / name).read_bytes()


@dataclass
class StubFetcher:
    """A ``Fetcher`` that replays canned responses.

    Args:
        payloads: Response body per location id.
        failures: Location ids that should raise ``FetchError``.
    """

    payloads: dict[str, bytes] = field(default_factory=dict)
    failures: set[str] = field(default_factory=set)
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def get(self, url: str, params: dict[str, str]) -> Response:
        self.calls.append((url, dict(params)))
        location = self._identify(params)
        if location in self.failures:
            raise FetchError(f"stubbed failure for {location}")
        default = fixture_bytes("open_meteo_colombo.json")
        return Response(
            status_code=200,
            content=self.payloads.get(location, default),
            url=url,
        )

    def _identify(self, params: dict[str, str]) -> str:
        """Map a request back to a location id by its latitude."""
        return _LATITUDE_TO_LOCATION.get(params.get("latitude", ""), "unknown")


_LATITUDE_TO_LOCATION = {
    "6.9271": "colombo",
    "6.0535": "galle",
    "8.5874": "trincomalee",
    "6.9497": "nuwara_eliya",
    "4.1755": "male",
}


@pytest.fixture
def locations() -> tuple[Location, ...]:
    return (
        Location("colombo", "Colombo", 6.9271, 79.8612, "Asia/Colombo"),
        Location("galle", "Galle", 6.0535, 80.2210, "Asia/Colombo"),
        Location("male", "Malé", 4.1755, 73.5093, "Indian/Maldives"),
    )


@pytest.fixture
def source_config(locations: tuple[Location, ...]) -> SourceConfig:
    return SourceConfig(
        id="open_meteo",
        enabled=True,
        endpoint="https://api.open-meteo.com/v1/forecast",
        daily_variables=("temperature_2m_max", "precipitation_sum"),
        locations=locations,
        timeout_seconds=5.0,
        max_attempts=2,
        backoff_seconds=0.0,
    )


@pytest.fixture
def stub_fetcher() -> StubFetcher:
    return StubFetcher()
