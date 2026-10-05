"""
Shared helpers for LLM rate-limit handling: 429 detection, retry-eligibility
classification, and a per-provider circuit breaker.

Used by article_scraper.py (per-article summary loop) and ai_reporter.py
(report-generation fallback chain) to avoid pounding a throttled provider
for the rest of a run.
"""

from __future__ import annotations

import urllib.error


_RATE_LIMIT_TOKENS = (
    "429",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "quota",
    "resource_exhausted",
    "resource exhausted",
    "too many requests",
)

_NON_RETRYABLE_STATUSES = {400, 401, 403, 404, 422}


def _status_code(exc: BaseException) -> int | None:
    """Best-effort extraction of an HTTP status code from a provider exception."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    if response is not None:
        rc = getattr(response, "status_code", None)
        if isinstance(rc, int):
            return rc
    return None


def is_rate_limit_error(exc: BaseException) -> bool:
    """True if the exception looks like a 429 / quota / rate-limit error."""
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
        return True
    if _status_code(exc) == 429:
        return True
    msg = str(exc).lower()
    return any(token in msg for token in _RATE_LIMIT_TOKENS)


def is_retryable_error(exc: BaseException) -> bool:
    """
    True if the call should be retried. Retries 429s, 5xx, and connection /
    timeout errors. Skips structural 4xx (auth, bad request) — burning the
    retry budget on those just slows everything down.
    """
    if is_rate_limit_error(exc):
        return True

    status = _status_code(exc)
    if status is not None:
        if status in _NON_RETRYABLE_STATUSES:
            return False
        if 500 <= status < 600:
            return True
        # Other 4xx → not retryable.
        if 400 <= status < 500:
            return False

    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return True

    return False


class ProviderCircuit:
    """
    Per-provider circuit breaker. Trips after `threshold` consecutive
    rate-limit hits; once tripped, `is_open(key)` returns True for the
    remainder of the run (callers should skip that provider entirely).
    A successful call resets the counter for that provider.
    """

    def __init__(self, threshold: int = 3) -> None:
        self.threshold = threshold
        self._failures: dict[str, int] = {}
        self._tripped: set[str] = set()

    def record_rate_limit(self, key: str) -> bool:
        """Record a 429 for `key`. Returns True if the circuit just tripped."""
        if key in self._tripped:
            return False
        self._failures[key] = self._failures.get(key, 0) + 1
        if self._failures[key] >= self.threshold:
            self._tripped.add(key)
            return True
        return False

    def record_success(self, key: str) -> None:
        self._failures[key] = 0

    def is_open(self, key: str) -> bool:
        return key in self._tripped
