import pytest

from llm_gateway.retry import backoff_seconds, is_retryable_status, parse_retry_after


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (500, True), (503, True), (400, False), (401, False), (404, False)],
)
def test_only_rate_limits_and_server_errors_are_retryable(status, retryable):
    assert is_retryable_status(status) is retryable


@pytest.mark.parametrize(
    ("header", "seconds"),
    [("3", 3.0), ("0.5", 0.5), (None, None), ("soon", None), ("-1", None), ("inf", None)],
)
def test_parse_retry_after(header, seconds):
    assert parse_retry_after(header) == seconds


def test_backoff_doubles_with_jitter_and_is_capped():
    for retry, full in [(0, 0.5), (1, 1.0), (2, 2.0), (6, 8.0)]:
        delay = backoff_seconds(retry, base=0.5, cap=8.0)
        assert full / 2 <= delay <= full


def test_backoff_honours_retry_after_up_to_the_cap():
    assert backoff_seconds(0, base=0.5, cap=8.0, retry_after=3.0) == 3.0
    assert backoff_seconds(0, base=0.5, cap=8.0, retry_after=30.0) is None
