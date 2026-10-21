import logging

import pytest

from autoshorts.gemini_calls import (
    GeminiPermanentError,
    GeminiRecoverableError,
    call_gemini,
)


class ApiError(Exception):
    def __init__(self, status_code, message="request failed"):
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize("failures", (1, 2))
def test_transient_failures_retry_then_succeed(failures):
    calls = []
    waits = []

    def request():
        calls.append(1)
        if len(calls) <= failures:
            raise ApiError(503)
        return "ok"

    result = call_gemini(
        operation="segment_analysis",
        model="configured-model",
        request=request,
        sleep=waits.append,
        jitter=lambda _low, _high: 0.0,
    )

    assert result == "ok"
    assert len(calls) == failures + 1
    assert waits == [1.0, 2.0][:failures]


@pytest.mark.parametrize("error", (ApiError(429), TimeoutError("timeout")))
def test_rate_limit_and_timeout_are_recoverable(error):
    with pytest.raises(GeminiRecoverableError):
        call_gemini(
            operation="metadata",
            model="configured-model",
            request=lambda: (_ for _ in ()).throw(error),
            sleep=lambda _delay: None,
            jitter=lambda _low, _high: 0.0,
        )


def test_three_503_failures_exhaust_once_without_secret_in_logs(caplog):
    calls = []
    secret = "super-secret-api-key"

    def request():
        calls.append(1)
        raise ApiError(503, f"unavailable {secret}")

    with caplog.at_level(logging.WARNING), pytest.raises(GeminiRecoverableError):
        call_gemini(
            operation="segment_analysis",
            model="configured-model",
            request=request,
            sleep=lambda _delay: None,
            jitter=lambda _low, _high: 0.0,
        )

    assert len(calls) == 3
    assert secret not in caplog.text
    assert "attempt=3/3" in caplog.text
    assert "retry=NAO" in caplog.text


def test_permanent_4xx_is_not_retried():
    calls = []

    def request():
        calls.append(1)
        raise ApiError(400)

    with pytest.raises(GeminiPermanentError):
        call_gemini(
            operation="metadata",
            model="configured-model",
            request=request,
        )
    assert len(calls) == 1
