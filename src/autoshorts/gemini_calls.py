"""Central retry and observability policy for Gemini API calls."""

from __future__ import annotations

import logging
import os
import random
import time
from collections.abc import Callable
from typing import TypeVar

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


class GeminiRecoverableError(RuntimeError):
    """A transient Gemini failure exhausted the bounded retry policy."""


class GeminiPermanentError(ValueError):
    """A non-retryable Gemini request failure."""


def create_gemini_client(*, api_key: str | None = None):
    """Create the official client with one SDK attempt and a bounded timeout."""
    from google import genai
    from google.genai import types

    timeout_ms = int(os.getenv("AUTOSHORTS_GEMINI_TIMEOUT_MS", "120000"))
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=timeout_ms,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


def call_gemini(
    *,
    operation: str,
    model: str,
    request: Callable[[], T],
    max_attempts: int = 3,
    sleep: Callable[[float], None] | None = None,
    jitter: Callable[[float, float], float] | None = None,
) -> T:
    """Run one logical Gemini operation with bounded transient retries."""
    if max_attempts < 1:
        raise ValueError("Gemini max_attempts must be positive.")
    sleeper = sleep or time.sleep
    jitter_value = jitter or random.uniform
    for attempt in range(1, max_attempts + 1):
        try:
            result = request()
        except Exception as exc:
            status = _status_code(exc)
            recoverable = _is_recoverable(exc, status)
            should_retry = recoverable and attempt < max_attempts
            delay = 0.0
            if should_retry:
                delay = (2 ** (attempt - 1)) + jitter_value(0.0, 0.25)
            LOGGER.warning(
                "Gemini operation=%s model=%s attempt=%s/%s result=%s "
                "retry=%s delay=%.2f",
                operation,
                model,
                attempt,
                max_attempts,
                status or type(exc).__name__,
                "SIM" if should_retry else "NAO",
                delay,
            )
            if should_retry:
                sleeper(delay)
                continue
            if recoverable:
                raise GeminiRecoverableError(
                    f"Gemini {operation} failed after {attempt} attempts "
                    f"({status or type(exc).__name__})."
                ) from exc
            raise GeminiPermanentError(
                f"Gemini {operation} rejected the request "
                f"({status or type(exc).__name__})."
            ) from exc
        LOGGER.info(
            "Gemini operation=%s model=%s attempt=%s/%s result=SUCCESS",
            operation,
            model,
            attempt,
            max_attempts,
        )
        return result
    raise AssertionError("unreachable")


def _status_code(exc: Exception) -> int | None:
    for value in (
        getattr(exc, "status_code", None),
        getattr(exc, "code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        if isinstance(value, int):
            return value
    text = str(exc).casefold()
    for code in (408, 429, 500, 502, 503, 504):
        if str(code) in text:
            return code
    return None


def _is_recoverable(exc: Exception, status: int | None) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    if status in {408, 429, 500, 502, 503, 504}:
        return True
    text = str(exc).casefold()
    return any(
        marker in text
        for marker in (
            "timeout",
            "timed out",
            "unavailable",
            "high demand",
            "connection reset",
            "connection aborted",
            "temporarily unavailable",
        )
    )
