"""When an upstream failure is worth retrying, and how long to wait first (decision 0006)."""

import math
import random


def is_retryable_status(status: int) -> bool:
    # 429 and 5xx are the provider being busy or broken right now. Other 4xx mean the request
    # itself is wrong (bad model name, bad key), so sending it again gets the same answer.
    return status == 429 or status >= 500


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a `Retry-After` header. The HTTP-date form is ignored; LLM APIs send seconds."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def backoff_seconds(
    retry: int, base: float, cap: float, retry_after: float | None = None
) -> float | None:
    """How long to wait before retry number `retry` (0 = the first retry).

    Returns None when the provider asked us to wait longer than `cap`: waiting that long would
    stall the client, so it's better to give up on this provider (and fall back) now.
    """
    if retry_after is not None:
        return retry_after if retry_after <= cap else None
    # Exponential, with jitter so retries from several requests don't all land at once.
    return min(cap, base * 2**retry) * random.uniform(0.5, 1.0)  # noqa: S311 - not crypto
