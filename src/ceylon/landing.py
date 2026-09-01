"""Raw landing — where bytes become files, and where idempotency lives.

Bronze is raw bytes as received (practice 26), so this module never parses a
payload. It only decides *where* bytes go and guarantees *how* they get there.

Two properties matter, and both are structural rather than bolted on:

**Deterministic paths.** The path is a pure function of source, run date and
location. Re-running a date rewrites the same files rather than accumulating
``_v2`` copies — which is what makes the job idempotent (practice 29) and makes
backfill the same code path as a normal run (practice 30).

**Atomic writes.** Content goes to a temporary file and is then renamed.
``os.replace`` is atomic within a filesystem, so a reader — Auto Loader, from
Stage 2 — never observes a half-written file, and a crash mid-write leaves the
previous version intact rather than a truncated one.

The layout mirrors the Unity Catalog volume it will move to in Stage 2, so that
change is a root path, not a rewrite::

    <root>/<source_id>/run_date=YYYY-MM-DD/<name>.json
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

__all__ = [
    "LandedFile",
    "PayloadSummary",
    "landing_dir",
    "landing_path",
    "summarise_payload",
    "write_atomic",
]

_RUN_DATE_PARTITION = "run_date"

# Fields a source varies on every call regardless of the data — server-side
# performance counters and the like. Declared volatile in the data contract.
# They are still stored verbatim in bronze; they are only excluded from the
# digest used to answer "did the data actually change?"
_VOLATILE_FIELDS = frozenset({"generationtime_ms"})


def landing_dir(root: Path, source_id: str, run_date: date) -> Path:
    """Directory holding one source's payloads for one run date."""
    return root / source_id / f"{_RUN_DATE_PARTITION}={run_date.isoformat()}"


def landing_path(root: Path, source_id: str, run_date: date, name: str) -> Path:
    """Full path for a single raw payload.

    Args:
        root: Landing root, e.g. ``data/landing``.
        source_id: Source identifier, e.g. ``open_meteo``.
        run_date: The date this payload describes.
        name: Payload name within the run, e.g. a location id.

    Returns:
        A deterministic path. Same inputs always give the same path.
    """
    return landing_dir(root, source_id, run_date) / f"{name}.json"


def write_atomic(path: Path, content: bytes) -> None:
    """Write bytes so a reader never sees a partial file.

    Args:
        path: Destination. Parent directories are created if absent.
        content: Exact bytes to write. Not transformed in any way.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # Same directory as the destination, so the rename stays within one
    # filesystem and is therefore atomic.
    handle, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@dataclass(frozen=True, slots=True)
class PayloadSummary:
    """Cheap facts about a payload, for the manifest and the logs.

    This is *description*, not validation. Bronze accepts whatever arrived; the
    DQ gates that reject it arrive in Stage 3. Recording these now means the
    gates have history to compare against when they do.
    """

    bytes_written: int
    sha256: str
    is_json: bool
    daily_row_count: int | None
    stable_sha256: str | None = None
    """Digest with volatile fields removed, or ``None`` when not JSON.

    ``sha256`` covers the bytes as stored, so two fetches of the same date
    disagree whenever the provider stamps a timing counter into the response.
    ``stable_sha256`` ignores those fields, so it answers the question that
    actually matters: did the *data* change? Stage 3 uses it to tell a genuine
    upstream revision from noise.
    """

    @property
    def is_empty(self) -> bool:
        """Whether the payload carried no daily rows."""
        return self.daily_row_count == 0


def summarise_payload(content: bytes) -> PayloadSummary:
    """Describe a raw payload without trusting it.

    Handles the failure modes a live source actually produces: a truncated body,
    a well-formed response with no rows, and a body that is not JSON at all
    (a provider error page, typically HTML).

    Args:
        content: Raw bytes as received.

    Returns:
        A summary. Never raises — a payload that cannot be understood is
        described as such, because bronze still stores it.
    """
    digest = hashlib.sha256(content).hexdigest()

    try:
        document = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return PayloadSummary(
            bytes_written=len(content),
            sha256=digest,
            is_json=False,
            daily_row_count=None,
        )

    row_count: int | None = None
    stable = document
    if isinstance(document, dict):
        daily = document.get("daily")
        if isinstance(daily, dict):
            times = daily.get("time")
            if isinstance(times, list):
                row_count = len(times)
        stable = {k: v for k, v in document.items() if k not in _VOLATILE_FIELDS}

    canonical = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()

    return PayloadSummary(
        bytes_written=len(content),
        sha256=digest,
        is_json=True,
        daily_row_count=row_count,
        stable_sha256=hashlib.sha256(canonical).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class LandedFile:
    """One payload that reached disk."""

    name: str
    path: Path
    summary: PayloadSummary
