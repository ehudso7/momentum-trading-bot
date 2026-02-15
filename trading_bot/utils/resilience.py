"""
Resilient API call utilities with retry logic, rate limiting, and error classification.

Provides decorators and helpers for making API calls robust against
transient failures, rate limits, and network issues.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timedelta
from enum import Enum
from functools import wraps
from typing import Any, Callable, Optional, TypeVar

import structlog

log = structlog.get_logger(__name__)

T = TypeVar("T")


class APIErrorType(str, Enum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    AUTH_ERROR = "auth_error"
    SERVER_ERROR = "server_error"
    CONNECTION_ERROR = "connection_error"
    UNKNOWN = "unknown"


def classify_error(error: Exception) -> APIErrorType:
    """Classify an API error by type for appropriate handling."""
    error_str = str(error).lower()
    error_type = type(error).__name__.lower()

    if any(x in error_str for x in ["timeout", "timed out", "deadline"]):
        return APIErrorType.TIMEOUT
    if any(x in error_str for x in ["rate limit", "429", "too many requests", "throttl"]):
        return APIErrorType.RATE_LIMIT
    if any(x in error_str for x in ["401", "403", "unauthorized", "forbidden", "auth"]):
        return APIErrorType.AUTH_ERROR
    if any(x in error_str for x in ["500", "502", "503", "504", "server error", "internal"]):
        return APIErrorType.SERVER_ERROR
    if any(x in error_type for x in ["connection", "network", "dns"]):
        return APIErrorType.CONNECTION_ERROR

    return APIErrorType.UNKNOWN


def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable_errors: tuple = (Exception,),
    on_retry: Optional[Callable] = None,
) -> Callable:
    """
    Decorator that retries a function with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay in seconds.
        max_delay: Maximum delay between retries.
        retryable_errors: Tuple of exception types to retry on.
        on_retry: Optional callback called on each retry with (attempt, error, delay).

    Usage:
        @retry_with_backoff(max_retries=3, base_delay=1.0)
        def fetch_data():
            return api.get(...)
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_error: Optional[Exception] = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_errors as e:
                    last_error = e
                    error_type = classify_error(e)

                    # Don't retry auth errors
                    if error_type == APIErrorType.AUTH_ERROR:
                        log.error(
                            "api.auth_error_no_retry",
                            func=func.__name__,
                            error=str(e),
                        )
                        raise

                    if attempt < max_retries:
                        delay = min(base_delay * (2**attempt), max_delay)

                        # Add extra delay for rate limits
                        if error_type == APIErrorType.RATE_LIMIT:
                            delay = max(delay, 10.0)

                        log.warning(
                            "api.retry",
                            func=func.__name__,
                            attempt=attempt + 1,
                            max_retries=max_retries,
                            delay=round(delay, 1),
                            error_type=error_type.value,
                            error=str(e)[:100],
                        )

                        if on_retry:
                            on_retry(attempt + 1, e, delay)

                        time.sleep(delay)
                    else:
                        log.error(
                            "api.max_retries_exceeded",
                            func=func.__name__,
                            attempts=max_retries + 1,
                            error_type=error_type.value,
                            error=str(e),
                        )

            raise last_error  # type: ignore[misc]

        return wrapper

    return decorator


class RateLimiter:
    """
    Token bucket rate limiter for API calls.

    Ensures calls don't exceed max_calls_per_minute.
    Thread-safe for single-threaded async-compatible use.
    """

    def __init__(self, max_calls_per_minute: int = 5):
        self._max_calls = max_calls_per_minute
        self._call_times: deque[float] = deque()
        self._window = 60.0  # 1 minute window

    def wait_if_needed(self) -> None:
        """Block until a call is allowed under the rate limit."""
        now = time.monotonic()

        # Remove calls outside the window
        while self._call_times and (now - self._call_times[0]) > self._window:
            self._call_times.popleft()

        if len(self._call_times) >= self._max_calls:
            # Wait until oldest call falls outside window
            wait_time = self._window - (now - self._call_times[0]) + 0.1
            if wait_time > 0:
                log.debug(
                    "rate_limiter.waiting",
                    wait_seconds=round(wait_time, 1),
                    calls_in_window=len(self._call_times),
                )
                time.sleep(wait_time)

        self._call_times.append(time.monotonic())

    def calls_remaining(self) -> int:
        """Number of calls remaining in current window."""
        now = time.monotonic()
        while self._call_times and (now - self._call_times[0]) > self._window:
            self._call_times.popleft()
        return max(0, self._max_calls - len(self._call_times))

    @property
    def is_limited(self) -> bool:
        return self.calls_remaining() == 0
