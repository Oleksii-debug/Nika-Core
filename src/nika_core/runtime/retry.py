from __future__ import annotations

from dataclasses import dataclass, field

from nika_core.runtime.contracts import RuntimeErrorCode, RuntimeOutcome, RuntimeResult


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Explicit, fail-closed retry policy for runtime failures.

    Retries are disabled by default. A caller must opt into exact error codes. By default
    a failed invocation must also expose a resume token so the coordinator can continue
    from durable runtime state instead of blindly replaying the original input.
    """

    max_retries: int = 0
    retryable_error_codes: frozenset[RuntimeErrorCode] = field(default_factory=frozenset)
    base_delay_seconds: float = 0.0
    max_delay_seconds: float = 30.0
    allow_fresh_retry: bool = False

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds must not be negative")
        if self.max_delay_seconds < 0:
            raise ValueError("max_delay_seconds must not be negative")
        if self.base_delay_seconds > self.max_delay_seconds:
            raise ValueError("base_delay_seconds must not exceed max_delay_seconds")

    def should_retry(self, result: RuntimeResult, *, retries_used: int) -> bool:
        if retries_used >= self.max_retries:
            return False
        if result.outcome != RuntimeOutcome.FAILED or result.error_code is None:
            return False
        if result.error_code not in self.retryable_error_codes:
            return False
        return result.resume_token is not None or self.allow_fresh_retry

    def delay_seconds(self, *, retry_number: int) -> float:
        """Return deterministic exponential backoff for a 1-based retry number."""

        if retry_number < 1:
            raise ValueError("retry_number must be positive")
        if self.base_delay_seconds == 0:
            return 0.0
        return min(
            self.base_delay_seconds * (2 ** (retry_number - 1)),
            self.max_delay_seconds,
        )
