"""HTTP retrieval with retries.

Extraction is the layer that touches the untrusted outside world, so it is the
layer that retries, times out and gives up loudly. It is also the layer tests
must not depend on: ``Fetcher`` is a protocol, so the whole pipeline runs offline
against a stub (practice 55).

Bronze stores bytes exactly as received, so nothing here parses a response.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx

from ceylon.observability import get_logger

__all__ = [
    "FetchError",
    "Fetcher",
    "HttpFetcher",
    "Response",
    "is_retryable_status",
]

_LOGGER = get_logger(__name__)

# Worth another attempt: the request may succeed unchanged. Anything else — 400,
# 401, 404 — is a contract breach that retrying only makes slower.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def is_retryable_status(status_code: int) -> bool:
    """Whether an HTTP status justifies another attempt."""
    return status_code in _RETRYABLE_STATUS


@dataclass(frozen=True, slots=True)
class Response:
    """A raw HTTP response. Deliberately not parsed."""

    status_code: int
    content: bytes
    url: str


class FetchError(RuntimeError):
    """Raised when a request fails after exhausting every attempt."""


class Fetcher(Protocol):
    """Anything that can turn a URL and parameters into raw bytes."""

    def get(self, url: str, params: dict[str, str]) -> Response:
        """Perform a GET, retrying transient failures.

        Raises:
            FetchError: On non-retryable failure or exhausted attempts.
        """
        ...


class HttpFetcher:
    """A ``Fetcher`` backed by httpx.

    Args:
        timeout_seconds: Per-attempt timeout. There is always a timeout; a
            hanging request is worse than a failing one because nothing alerts.
        max_attempts: Total attempts including the first.
        backoff_seconds: Base delay, doubled per attempt with jitter so retries
            from concurrent sources do not synchronise onto the provider.
        client: Injectable for tests.
        sleep: Injectable so tests do not actually wait.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_attempts: int = 4,
        backoff_seconds: float = 1.0,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds
        self._client = client
        self._sleep = sleep if sleep is not None else time.sleep

    def _delay(self, attempt: int) -> float:
        # Jittered exponential backoff. Not cryptographic — the point is to
        # desynchronise concurrent retries, not to be unpredictable.
        jitter = 0.5 + random.random()
        return float(self._backoff * (2 ** (attempt - 1)) * jitter)

    def get(self, url: str, params: dict[str, str]) -> Response:
        """Perform a GET, retrying transient failures.

        Args:
            url: Absolute URL.
            params: Query parameters.

        Returns:
            The raw response.

        Raises:
            FetchError: On a non-retryable status or once attempts are exhausted.
        """
        client = self._client or httpx.Client(timeout=self._timeout)
        owns_client = self._client is None
        last_error: str = "no attempt was made"

        try:
            for attempt in range(1, self._max_attempts + 1):
                try:
                    raw = client.get(url, params=params)
                except httpx.RequestError as exc:  # timeout, DNS, connection reset
                    last_error = f"{type(exc).__name__}: {exc}"
                    retryable = True
                else:
                    if raw.status_code < 400:
                        return Response(
                            status_code=raw.status_code,
                            content=raw.content,
                            url=str(raw.url),
                        )
                    last_error = f"HTTP {raw.status_code}"
                    retryable = is_retryable_status(raw.status_code)

                if not retryable:
                    raise FetchError(f"{url}: {last_error} (not retryable)")

                if attempt < self._max_attempts:
                    delay = self._delay(attempt)
                    _LOGGER.warning(
                        "fetch.retry",
                        extra={
                            "url": url,
                            "attempt": attempt,
                            "max_attempts": self._max_attempts,
                            "reason": last_error,
                            "delay_seconds": round(delay, 3),
                        },
                    )
                    self._sleep(delay)

            raise FetchError(
                f"{url}: giving up after {self._max_attempts} attempts; "
                f"last error: {last_error}"
            )
        finally:
            if owns_client:
                client.close()
