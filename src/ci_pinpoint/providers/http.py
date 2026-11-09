"""HTTP plumbing shared by the hosted providers: timeouts, retries and backoff."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ci_pinpoint.providers.base import ProviderError

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 20.0

    def delay(self, attempt: int, retry_after: float | None, rng: random.Random) -> float:
        """Full-jitter exponential backoff, but never sooner than Retry-After."""
        ceiling = min(self.max_delay_s, self.base_delay_s * (2**attempt))
        delay = rng.uniform(0, ceiling)
        if retry_after is not None:
            delay = max(delay, min(retry_after, self.max_delay_s))
        return delay


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            return str(error.get("message") or error)[:300]
        return str(error)[:300]
    return str(body)[:300]


class JsonHttpClient:
    """POST JSON with retries. One instance per provider; reuses connections."""

    def __init__(
        self,
        *,
        timeout_s: float = 120.0,
        retry: RetryPolicy | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=min(10.0, timeout_s)),
            transport=transport,
            follow_redirects=True,
        )
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not crypto

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        what: str = "request",
    ) -> dict[str, Any]:
        last_error: ProviderError | None = None
        for attempt in range(self._retry.max_retries + 1):
            retry_after: float | None = None
            try:
                response = self._client.post(url, json=payload, headers=headers, params=params)
            except httpx.TimeoutException as exc:
                last_error = ProviderError(f"{what} timed out: {exc}", retryable=True)
            except httpx.TransportError as exc:
                last_error = ProviderError(f"{what} failed to connect: {exc}", retryable=True)
            else:
                if response.status_code < 400:
                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise ProviderError(f"{what} returned non-JSON: {exc}") from exc
                    if not isinstance(data, dict):
                        raise ProviderError(
                            f"{what} returned unexpected JSON: {type(data).__name__}"
                        )
                    return data
                retryable = response.status_code in RETRYABLE_STATUS
                last_error = ProviderError(
                    f"{what} failed with HTTP {response.status_code}: {_error_detail(response)}",
                    status=response.status_code,
                    retryable=retryable,
                )
                retry_after = parse_retry_after(response.headers.get("retry-after"))
            if not last_error.retryable or attempt == self._retry.max_retries:
                break
            delay = self._retry.delay(attempt, retry_after, self._rng)
            log.warning(
                "provider call failed, retrying",
                extra={
                    "what": what,
                    "attempt": attempt + 1,
                    "delay_s": round(delay, 2),
                    "error": str(last_error),
                },
            )
            self._sleep(delay)
        assert last_error is not None
        raise last_error

    def close(self) -> None:
        self._client.close()
