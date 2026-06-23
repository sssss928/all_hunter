"""Reusable retry and backoff policies for transient UI failures."""

from __future__ import annotations

from dataclasses import dataclass
import random


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff with optional jitter."""

    max_attempts: int = 5
    initial_delay: float = 0.25
    multiplier: float = 1.8
    max_delay: float = 5.0
    jitter: float = 0.1

    def delay_for(self, attempt: int) -> float:
        """Return the delay before a one-based retry attempt."""

        normalized_attempt = max(1, int(attempt))
        base = min(
            self.max_delay,
            self.initial_delay * (self.multiplier ** (normalized_attempt - 1)),
        )
        if self.jitter <= 0:
            return base
        spread = base * min(self.jitter, 1.0)
        return max(0.0, base + random.uniform(-spread, spread))

    def allows(self, attempt: int) -> bool:
        return 1 <= int(attempt) <= self.max_attempts
