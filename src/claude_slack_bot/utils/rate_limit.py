from __future__ import annotations

from slack_sdk.errors import SlackApiError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


def is_rate_limited(exc: BaseException) -> bool:
    if isinstance(exc, SlackApiError):
        return exc.response.status_code == 429
    return False


slack_retry = retry(
    retry=retry_if_exception_type(SlackApiError),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
