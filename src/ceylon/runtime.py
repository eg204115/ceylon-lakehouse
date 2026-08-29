"""Run identity and run-date handling.

Every pipeline invocation is identified by a ``run_id`` and scoped to a ``run_date``.
Both exist from the first commit rather than being retrofitted, because they thread
through logging, storage paths and the ``run_metadata`` table — and threading a
correlation id through code that was written without one is a rewrite.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from uuid import uuid4

__all__ = ["RunDateError", "new_run_id", "parse_run_date"]

# Strict YYYY-MM-DD. `date.fromisoformat` alone is too permissive — it accepts
# "20260801" and, from 3.11, full timestamps — which lets an ambiguous CLI argument
# silently select the wrong partition.
_RUN_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_RUN_ID_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"


class RunDateError(ValueError):
    """Raised when a run date is not a strict ISO ``YYYY-MM-DD`` calendar date."""


def parse_run_date(value: str) -> date:
    """Parse a strict ISO ``YYYY-MM-DD`` run date.

    Args:
        value: The date string, typically from ``--run-date``.

    Returns:
        The parsed calendar date.

    Raises:
        RunDateError: If the value is not exactly ``YYYY-MM-DD``, or is not a real
            date such as ``2026-02-30``.

    Examples:
        >>> parse_run_date("2026-08-01")
        datetime.date(2026, 8, 1)
    """
    if not _RUN_DATE_PATTERN.match(value):
        raise RunDateError(
            f"run date must be YYYY-MM-DD, got {value!r}",
        )
    try:
        return date.fromisoformat(value)
    except ValueError as exc:  # e.g. 2026-02-30
        raise RunDateError(f"run date is not a real date: {value!r}") from exc


def new_run_id(now: datetime | None = None) -> str:
    """Generate a correlation id for one pipeline invocation.

    The id is lexicographically sortable by time, so sorting run ids sorts runs, and
    the random suffix keeps two runs started in the same second distinct.

    Args:
        now: Timestamp to embed. Defaults to the current UTC time. Injectable so
            tests do not depend on the clock.

    Returns:
        An id of the form ``20260801T134500Z-1a2b3c4d``.

    Raises:
        ValueError: If ``now`` is timezone-naive. A naive timestamp in a pipeline is
            a latent correctness bug, not a formatting detail.
    """
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    stamp = moment.astimezone(UTC).strftime(_RUN_ID_TIMESTAMP_FORMAT)
    return f"{stamp}-{uuid4().hex[:8]}"
