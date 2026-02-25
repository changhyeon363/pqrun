"""Retry and idle polling policies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class BackoffPolicy:
    """
    Retry delay policy for failed jobs.

    Default exponential backoff:
      - Attempt 1 → 1 minute
      - Attempt 2 → 5 minutes
      - Attempt 3 → 30 minutes
      - Attempt 4 → 2 hours
      - Attempt 5+ → 6 hours

    Override retry_delay() for custom policies.
    """

    def retry_delay(self, attempts: int) -> timedelta:
        """
        Calculate retry delay based on number of attempts.

        Args:
            attempts: Number of attempts already made (1-indexed)

        Returns:
            timedelta to wait before next retry
        """
        if attempts <= 1:
            return timedelta(minutes=1)
        if attempts == 2:
            return timedelta(minutes=5)
        if attempts == 3:
            return timedelta(minutes=30)
        if attempts == 4:
            return timedelta(hours=2)
        return timedelta(hours=6)


@dataclass(frozen=True)
class IdlePollPolicy:
    """
    Polling backoff when no jobs are available.

    Gradually increases sleep duration to reduce DB load:
      - Empty streak 0 → base_seconds (default: 1s)
      - Empty streak 1 → 2s
      - Empty streak 2 → 5s
      - Empty streak 3+ → max_seconds (default: 10s)

    Resets to base_seconds when a job is found.
    """

    base_seconds: float = 1.0
    max_seconds: float = 10.0

    def next_sleep(self, empty_streak: int) -> float:
        """
        Calculate sleep duration based on consecutive empty pickups.

        Args:
            empty_streak: Number of consecutive empty pickup() calls

        Returns:
            Sleep duration in seconds
        """
        if empty_streak <= 0:
            return self.base_seconds
        if empty_streak == 1:
            return min(2.0, self.max_seconds)
        if empty_streak == 2:
            return min(5.0, self.max_seconds)
        return self.max_seconds


@dataclass(frozen=True)
class LoopErrorPolicy:
    """
    Retry policy for worker loop-level infrastructure errors.

    This controls sleep after unexpected errors outside handler logic
    (e.g., pickup/mark_* DB failures). Default is immediate retry.
    """

    def next_sleep(self, consecutive_errors: int) -> float:
        """
        Calculate sleep before next loop iteration after infra error.

        Args:
            consecutive_errors: Number of consecutive loop-level errors

        Returns:
            Sleep duration in seconds
        """
        return 0.0
