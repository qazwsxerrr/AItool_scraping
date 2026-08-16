"""Small, shared retry primitive for item-level AI provider calls."""

from __future__ import annotations

import time
from typing import Any, Callable, TypeVar


DEFAULT_PROVIDER_MAX_RETRIES = 5
"""Number of retries after the initial provider call."""

_RETRY_BACKOFF_BASE_SECONDS = 0.1
_RETRY_BACKOFF_MAX_SECONDS = 1.0
_T = TypeVar("_T")


class ProviderResponseFailure(RuntimeError):
    """Turn a structured provider failure response into a retryable error."""

    def __init__(self, result: Any):
        self.result = result
        self.error_code = getattr(result, "error_code", None)
        self.error_message = getattr(result, "error_message", None)
        self.raw_response = getattr(result, "raw_response", None)
        super().__init__(str(self.error_message or "provider returned a failed result"))


class ProviderRetryExhausted(RuntimeError):
    """Terminal error emitted after all configured provider retries fail."""

    retry_exhausted = True

    def __init__(self, *, stage: str, attempts: int, cause: BaseException):
        self.stage = stage
        self.attempts = int(attempts)
        self.cause = cause
        self.status_code = getattr(cause, "status_code", None)
        self.error_code = getattr(cause, "error_code", None) or (
            str(self.status_code) if self.status_code is not None else None
        )
        self.raw_response = getattr(cause, "raw_response", None)
        super().__init__(
            f"{stage} provider retry exhausted after {self.attempts} attempts: {cause}"
        )


def call_with_provider_retries(
    operation: Callable[[], _T],
    *,
    is_retryable: Callable[[BaseException], bool],
    stage: str,
    max_retries: int = DEFAULT_PROVIDER_MAX_RETRIES,
    sleeper: Callable[[float], Any] = time.sleep,
) -> tuple[_T | None, BaseException | None, int]:
    """Run one provider operation with bounded transient-error retries.

    The returned attempt count includes the initial call.  Permanent errors
    are returned immediately; transient errors are retried with a small
    exponential backoff.  Exhaustion returns ``ProviderRetryExhausted`` so the
    caller can persist a terminal task failure instead of leaving it waiting.
    """

    retry_limit = max(0, int(max_retries))
    attempts = 0
    while True:
        attempts += 1
        try:
            return operation(), None, attempts
        except BaseException as exc:
            retryable = bool(is_retryable(exc))
            if not retryable:
                return None, exc, attempts
            if attempts > retry_limit:
                return None, ProviderRetryExhausted(stage=stage, attempts=attempts, cause=exc), attempts
            delay = min(
                _RETRY_BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)),
                _RETRY_BACKOFF_MAX_SECONDS,
            )
            sleeper(delay)


__all__ = [
    "DEFAULT_PROVIDER_MAX_RETRIES",
    "ProviderResponseFailure",
    "ProviderRetryExhausted",
    "call_with_provider_retries",
]
