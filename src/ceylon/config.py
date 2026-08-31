"""Source configuration loading and validation.

Configuration is data in ``config/sources.yml``, not literals in code. It is
validated on load and fails loudly: a pipeline that starts with a longitude of
790.0 and discovers it three layers later has wasted a run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "ConfigError",
    "Location",
    "SourceConfig",
    "SourcesConfig",
    "load_sources",
]

_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_ATTEMPTS = 4
_DEFAULT_BACKOFF_SECONDS = 1.0


class ConfigError(ValueError):
    """Raised when configuration is missing, malformed or out of range."""


@dataclass(frozen=True, slots=True)
class Location:
    """A geographic point to collect weather for."""

    id: str
    name: str
    latitude: float
    longitude: float
    timezone: str


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """Wiring for one source."""

    id: str
    enabled: bool
    endpoint: str
    daily_variables: tuple[str, ...]
    locations: tuple[Location, ...]
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS
    backoff_seconds: float = _DEFAULT_BACKOFF_SECONDS


@dataclass(frozen=True, slots=True)
class SourcesConfig:
    """All configured sources, keyed by id."""

    version: int
    sources: dict[str, SourceConfig] = field(default_factory=dict)

    def get(self, source_id: str) -> SourceConfig:
        """Return one source's config.

        Raises:
            ConfigError: If the source is not configured, listing what is.
        """
        try:
            return self.sources[source_id]
        except KeyError:
            known = ", ".join(sorted(self.sources)) or "<none>"
            raise ConfigError(
                f"unknown source {source_id!r}; configured sources: {known}"
            ) from None


def _require(mapping: dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{context}: missing required key {key!r}")
    return mapping[key]


def _as_float(value: Any, key: str, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{context}: {key} must be a number, got {value!r}")
    return float(value)


def _parse_location(raw: Any, context: str) -> Location:
    if not isinstance(raw, dict):
        raise ConfigError(f"{context}: each location must be a mapping, got {raw!r}")

    location_id = str(_require(raw, "id", context))
    where = f"{context}.{location_id}"

    latitude = _as_float(_require(raw, "latitude", where), "latitude", where)
    longitude = _as_float(_require(raw, "longitude", where), "longitude", where)
    if not -90.0 <= latitude <= 90.0:
        raise ConfigError(f"{where}: latitude {latitude} outside [-90, 90]")
    if not -180.0 <= longitude <= 180.0:
        raise ConfigError(f"{where}: longitude {longitude} outside [-180, 180]")

    return Location(
        id=location_id,
        name=str(_require(raw, "name", where)),
        latitude=latitude,
        longitude=longitude,
        timezone=str(_require(raw, "timezone", where)),
    )


def _parse_source(source_id: str, raw: Any, defaults: dict[str, Any]) -> SourceConfig:
    context = f"sources.{source_id}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{context}: must be a mapping, got {raw!r}")

    variables = _require(raw, "daily_variables", context)
    if not isinstance(variables, list) or not variables:
        raise ConfigError(f"{context}: daily_variables must be a non-empty list")

    raw_locations = _require(raw, "locations", context)
    if not isinstance(raw_locations, list) or not raw_locations:
        raise ConfigError(f"{context}: locations must be a non-empty list")

    locations = tuple(
        _parse_location(item, f"{context}.locations") for item in raw_locations
    )
    seen = [location.id for location in locations]
    duplicates = sorted({item for item in seen if seen.count(item) > 1})
    if duplicates:
        raise ConfigError(f"{context}: duplicate location ids: {', '.join(duplicates)}")

    def setting(key: str, fallback: object) -> object:
        """Source value, else file-level default, else the built-in."""
        return raw.get(key, defaults.get(key, fallback))

    timeout = _as_float(
        setting("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS), "timeout_seconds", context
    )
    attempts = setting("max_attempts", _DEFAULT_MAX_ATTEMPTS)
    backoff = _as_float(
        setting("backoff_seconds", _DEFAULT_BACKOFF_SECONDS), "backoff_seconds", context
    )
    if timeout <= 0:
        raise ConfigError(f"{context}: timeout_seconds must be positive, got {timeout}")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        raise ConfigError(f"{context}: max_attempts must be an integer >= 1")
    if backoff < 0:
        raise ConfigError(f"{context}: backoff_seconds must not be negative")

    return SourceConfig(
        id=source_id,
        enabled=bool(raw.get("enabled", True)),
        endpoint=str(_require(raw, "endpoint", context)),
        daily_variables=tuple(str(item) for item in variables),
        locations=locations,
        timeout_seconds=timeout,
        max_attempts=attempts,
        backoff_seconds=backoff,
    )


def load_sources(path: Path) -> SourcesConfig:
    """Load and validate ``config/sources.yml``.

    Args:
        path: Path to the YAML file.

    Returns:
        The validated configuration.

    Raises:
        ConfigError: If the file is missing, is not a mapping, or any source
            fails validation.
    """
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc

    if not isinstance(document, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    raw_sources = document.get("sources")
    if not isinstance(raw_sources, dict) or not raw_sources:
        raise ConfigError(f"{path}: 'sources' must be a non-empty mapping")

    raw_defaults = document.get("defaults") or {}
    if not isinstance(raw_defaults, dict):
        raise ConfigError(f"{path}: 'defaults' must be a mapping")

    return SourcesConfig(
        version=int(document.get("version", 1)),
        sources={
            source_id: _parse_source(source_id, raw, raw_defaults)
            for source_id, raw in raw_sources.items()
        },
    )
