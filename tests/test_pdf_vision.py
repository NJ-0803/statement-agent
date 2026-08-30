"""Offline tests for pdf_vision's retry helper — no live API calls. Added alongside
concurrent multi-page vision extraction in pipeline.py: retrying a transient failure
(rate limit, overload, connection blip) matters more once several pages fire vision
calls at once, since a burst of concurrent requests is exactly what's more likely to
trip a rate limit than one page at a time was.
"""

import anthropic
import pytest

from statement_agent.ingest.pdf_vision import _with_retry


class _FakeResponse:
    status_code = 429
    headers = {}
    request = None


def _make_rate_limit_error():
    return anthropic.RateLimitError("rate limited", response=_FakeResponse(), body=None)


def _make_bad_request_error():
    resp = _FakeResponse()
    resp.status_code = 400
    return anthropic.BadRequestError("bad request", response=resp, body=None)


class TestRetrySucceedsAfterTransientFailures:
    def test_succeeds_on_second_attempt_after_one_retryable_failure(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _: None)  # no real delay in tests
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise _make_rate_limit_error()
            return "ok"

        result = _with_retry(flaky, max_attempts=3, base_delay=0.01)
        assert result == "ok"
        assert calls["n"] == 2

    def test_gives_up_after_max_attempts_and_raises_the_last_error(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _: None)
        calls = {"n": 0}

        def always_fails():
            calls["n"] += 1
            raise _make_rate_limit_error()

        with pytest.raises(anthropic.RateLimitError):
            _with_retry(always_fails, max_attempts=3, base_delay=0.01)
        assert calls["n"] == 3  # every attempt was actually made, not silently skipped


class TestNonRetryableErrorsFailImmediately:
    def test_bad_request_error_is_never_retried(self, monkeypatch):
        # retrying a 400 just burns time/API calls for an error that will fail
        # identically every time — must raise on the very first attempt
        monkeypatch.setattr("time.sleep", lambda _: None)
        calls = {"n": 0}

        def bad_request():
            calls["n"] += 1
            raise _make_bad_request_error()

        with pytest.raises(anthropic.BadRequestError):
            _with_retry(bad_request, max_attempts=3, base_delay=0.01)
        assert calls["n"] == 1


class TestNoRetryNeededOnFirstSuccess:
    def test_succeeds_immediately_without_any_delay(self, monkeypatch):
        def sleep_should_not_be_called(_):
            raise AssertionError("time.sleep should never be called when the first attempt succeeds")

        monkeypatch.setattr("time.sleep", sleep_should_not_be_called)
        assert _with_retry(lambda: "ok", max_attempts=3, base_delay=0.01) == "ok"
