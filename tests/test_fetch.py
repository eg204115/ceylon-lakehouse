"""Tests for HTTP retrieval and its retry policy."""

from __future__ import annotations

import httpx
import pytest

from ceylon.fetch import FetchError, HttpFetcher, is_retryable_status

URL = "https://example.invalid/forecast"


def fetcher(handler: httpx.MockTransport, **kwargs: object) -> HttpFetcher:
    """Build a fetcher whose sleeps are recorded rather than performed."""
    return HttpFetcher(
        client=httpx.Client(transport=handler),
        sleep=kwargs.pop("sleep", lambda _seconds: None),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


class TestRetryPolicy:
    @pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
    def test_transient_statuses_are_retryable(self, status: int) -> None:
        assert is_retryable_status(status)

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_client_errors_are_not_retryable(self, status: int) -> None:
        # Retrying a 404 just fails more slowly.
        assert not is_retryable_status(status)


class TestHttpFetcher:
    def test_returns_content_on_success(self) -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b'{"ok": true}')
        )
        response = fetcher(transport).get(URL, {"a": "1"})
        assert response.status_code == 200
        assert response.content == b'{"ok": true}'

    def test_does_not_parse_the_body(self) -> None:
        # Bronze stores bytes. Anything that parses here is a layer violation.
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"not json at all")
        )
        assert fetcher(transport).get(URL, {}).content == b"not json at all"

    def test_retries_a_transient_failure_then_succeeds(self) -> None:
        attempts = {"count": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            if attempts["count"] < 3:
                return httpx.Response(503)
            return httpx.Response(200, content=b"recovered")

        response = fetcher(httpx.MockTransport(handler), max_attempts=4).get(URL, {})
        assert response.content == b"recovered"
        assert attempts["count"] == 3

    def test_gives_up_after_the_attempt_budget(self) -> None:
        attempts = {"count": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            return httpx.Response(503)

        with pytest.raises(FetchError, match="giving up after 3 attempts"):
            fetcher(httpx.MockTransport(handler), max_attempts=3).get(URL, {})
        assert attempts["count"] == 3

    def test_does_not_retry_a_client_error(self) -> None:
        attempts = {"count": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            return httpx.Response(404)

        with pytest.raises(FetchError, match="not retryable"):
            fetcher(httpx.MockTransport(handler), max_attempts=5).get(URL, {})
        assert attempts["count"] == 1

    def test_retries_a_connection_error(self) -> None:
        attempts = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            if attempts["count"] < 2:
                raise httpx.ConnectTimeout("timed out", request=request)
            return httpx.Response(200, content=b"ok")

        assert fetcher(httpx.MockTransport(handler)).get(URL, {}).content == b"ok"
        assert attempts["count"] == 2

    def test_backoff_grows_between_attempts(self) -> None:
        delays: list[float] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        with pytest.raises(FetchError):
            fetcher(
                httpx.MockTransport(handler),
                max_attempts=4,
                backoff_seconds=1.0,
                sleep=delays.append,
            ).get(URL, {})

        assert len(delays) == 3
        # Jittered, so assert the trend rather than exact values.
        assert delays[0] < delays[-1]

    def test_sends_the_given_parameters(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return httpx.Response(200, content=b"{}")

        fetcher(httpx.MockTransport(handler)).get(URL, {"latitude": "6.9271"})
        assert seen["latitude"] == "6.9271"
