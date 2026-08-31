"""Tests for configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from ceylon.config import ConfigError, load_sources

VALID = """
version: 1
defaults:
  timeout_seconds: 10.0
  max_attempts: 3
sources:
  open_meteo:
    enabled: true
    endpoint: https://example.invalid/forecast
    daily_variables: [temperature_2m_max]
    locations:
      - {id: colombo, name: Colombo, latitude: 6.9271, longitude: 79.8612,
         timezone: Asia/Colombo}
"""


def write_config(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "sources.yml"
    path.write_text(content, encoding="utf-8")
    return path


class TestLoadSources:
    def test_loads_the_real_project_config(self) -> None:
        # The config the CLI actually uses must parse. A test that only ever
        # reads synthetic YAML would not notice a typo in the committed file.
        config = load_sources(Path("config/sources.yml"))
        source = config.get("open_meteo")
        assert source.enabled
        assert len(source.locations) == 5
        assert {location.id for location in source.locations} == {
            "colombo",
            "galle",
            "trincomalee",
            "nuwara_eliya",
            "male",
        }

    def test_committed_config_matches_the_contract_locations(self) -> None:
        # Config and contract drifting apart is the failure this catches.
        import yaml

        contract = yaml.safe_load(
            Path("contracts/open_meteo.yml").read_text(encoding="utf-8")
        )
        contract_ids = {entry["id"] for entry in contract["locations"]}
        config_ids = {
            location.id
            for location in load_sources(Path("config/sources.yml"))
            .get("open_meteo")
            .locations
        }
        assert config_ids == contract_ids

    def test_applies_defaults(self, tmp_path: Path) -> None:
        source = load_sources(write_config(tmp_path, VALID)).get("open_meteo")
        assert source.timeout_seconds == 10.0
        assert source.max_attempts == 3

    def test_source_overrides_beat_defaults(self, tmp_path: Path) -> None:
        content = VALID.replace(
            "    enabled: true", "    enabled: true\n    timeout_seconds: 99.0"
        )
        source = load_sources(write_config(tmp_path, content)).get("open_meteo")
        assert source.timeout_seconds == 99.0


class TestValidation:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_sources(tmp_path / "absent.yml")

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="invalid YAML"):
            load_sources(write_config(tmp_path, "key: [unclosed"))

    def test_top_level_must_be_a_mapping(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="top level must be a mapping"):
            load_sources(write_config(tmp_path, "- just\n- a\n- list\n"))

    def test_sources_must_be_present(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="non-empty mapping"):
            load_sources(write_config(tmp_path, "version: 1\n"))

    def test_unknown_source_lists_what_is_available(self, tmp_path: Path) -> None:
        config = load_sources(write_config(tmp_path, VALID))
        with pytest.raises(ConfigError, match="configured sources: open_meteo"):
            config.get("nonexistent")

    @pytest.mark.parametrize(
        ("bad", "expected"),
        [
            ("latitude: 6.9271", "latitude: 99.0"),
            ("longitude: 79.8612", "longitude: 500.0"),
        ],
    )
    def test_rejects_out_of_range_coordinates(
        self, tmp_path: Path, bad: str, expected: str
    ) -> None:
        with pytest.raises(ConfigError, match="outside"):
            load_sources(write_config(tmp_path, VALID.replace(bad, expected)))

    def test_rejects_a_non_numeric_coordinate(self, tmp_path: Path) -> None:
        content = VALID.replace("latitude: 6.9271", "latitude: north")
        with pytest.raises(ConfigError, match="must be a number"):
            load_sources(write_config(tmp_path, content))

    def test_rejects_empty_locations(self, tmp_path: Path) -> None:
        content = VALID.split("    locations:")[0] + "    locations: []\n"
        with pytest.raises(ConfigError, match="locations must be a non-empty list"):
            load_sources(write_config(tmp_path, content))

    def test_rejects_empty_variables(self, tmp_path: Path) -> None:
        content = VALID.replace(
            "daily_variables: [temperature_2m_max]", "daily_variables: []"
        )
        with pytest.raises(ConfigError, match="daily_variables must be a non-empty"):
            load_sources(write_config(tmp_path, content))

    def test_rejects_duplicate_location_ids(self, tmp_path: Path) -> None:
        duplicate = (
            "      - {id: colombo, name: Again, latitude: 7.0, "
            "longitude: 80.0, timezone: Asia/Colombo}\n"
        )
        with pytest.raises(ConfigError, match="duplicate location ids: colombo"):
            load_sources(write_config(tmp_path, VALID + duplicate))

    def test_rejects_a_missing_required_key(self, tmp_path: Path) -> None:
        content = VALID.replace("    endpoint: https://example.invalid/forecast\n", "")
        with pytest.raises(ConfigError, match="missing required key 'endpoint'"):
            load_sources(write_config(tmp_path, content))

    @pytest.mark.parametrize(
        ("override", "expected"),
        [
            ("timeout_seconds: 10.0", "timeout_seconds: -1.0"),
            ("max_attempts: 3", "max_attempts: 0"),
        ],
    )
    def test_rejects_nonsensical_retry_settings(
        self, tmp_path: Path, override: str, expected: str
    ) -> None:
        with pytest.raises(ConfigError):
            load_sources(write_config(tmp_path, VALID.replace(override, expected)))
