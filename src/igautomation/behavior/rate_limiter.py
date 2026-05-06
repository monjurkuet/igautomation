"""Async rate limiter with exponential backoff for IG API calls.

Designed for the daemon loop — uses asyncio.sleep instead of time.sleep.
Tracks consecutive errors, applies exponential backoff with jitter, and
enters a cooldown mode when too many errors pile up.

Usage::

    limiter = RateLimiter(RateLimitConfig(base_delay=2.0))
    await limiter.acquire()       # waits as needed
    try:
        result = await some_ig_call()
        limiter.record_success()
    except RateLimitError:
        limiter.record_rate_limit()
    except Exception:
        limiter.record_error("api")
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------


class RateLimitConfig(BaseModel):
    """Tunable knobs for the rate limiter."""

    base_delay: float = Field(
        1.0,
        description="Minimum seconds between API calls.",
        ge=0.01,
    )
    max_delay: float = Field(
        300.0,
        description="Cap on backoff delay (seconds).",
        ge=1.0,
    )
    backoff_factor: float = Field(
        2.0,
        description="Multiply current_delay by this on each error.",
        ge=1.0,
    )
    jitter_range: float = Field(
        0.1,
        description="Fraction of delay to add as random jitter (0-1).",
        ge=0.0,
        le=1.0,
    )
    cooldown_threshold: int = Field(
        5,
        description="Consecutive errors before entering cooldown.",
        ge=1,
    )
    cooldown_duration: float = Field(
        60.0,
        description="Seconds to wait when in cooldown mode.",
        ge=0.01,
    )
    recovery_factor: float = Field(
        0.5,
        description="After success, multiply current_delay by this.",
        gt=0.0,
        le=1.0,
    )
    max_concurrent: int = Field(
        1,
        description="Max concurrent API calls (semaphore size).",
        ge=1,
    )


# ------------------------------------------------------------------
# RateLimiter
# ------------------------------------------------------------------


class RateLimiter:
    """Async rate limiter with exponential backoff and cooldown.

    Call ``acquire()`` before every IG API call — it will wait as
    needed to respect rate limits.  Then call ``record_success()`` or
    ``record_error()`` / ``record_rate_limit()`` depending on the
    outcome.
    """

    def __init__(self, config: RateLimitConfig | None = None) -> None:
        self._config = config or RateLimitConfig()

        # Backoff state
        self._current_delay: float = self._config.base_delay
        self._consecutive_errors: int = 0
        self._in_cooldown: bool = False
        self._cooldown_until: float = 0.0  # monotonic timestamp

        # Rolling counters
        self._total_calls: int = 0
        self._total_errors: int = 0
        self._total_successes: int = 0

        # Concurrency
        self._semaphore = asyncio.Semaphore(self._config.max_concurrent)

        # Last-call timestamp for minimum inter-call spacing
        self._last_call_at: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def acquire(self) -> None:
        """Wait until an API call is permitted.

        Respects:
        1.  Cooldown mode (long sleep if too many errors)
        2.  Minimum inter-call delay (``current_delay``)
        3.  Concurrency semaphore
        """
        await self._semaphore.acquire()

        # 1. Cooldown mode
        if self._in_cooldown:
            remaining = self._cooldown_until - time.monotonic()
            if remaining > 0:
                logger.warning(
                    "RateLimiter in cooldown — waiting %.1fs",
                    remaining,
                )
                await asyncio.sleep(remaining)
            self._in_cooldown = False

        # 2. Inter-call delay
        now = time.monotonic()
        elapsed = now - self._last_call_at
        wait = self._jittered_delay() - elapsed
        if wait > 0:
            logger.debug("RateLimiter waiting %.2fs (delay=%.2fs, elapsed=%.2fs)",
                         wait, self._current_delay, elapsed)
            await asyncio.sleep(wait)

        self._last_call_at = time.monotonic()
        self._total_calls += 1

    def release(self) -> None:
        """Release the concurrency semaphore (call after API call completes)."""
        self._semaphore.release()

    async def __aenter__(self) -> RateLimiter:
        await self.acquire()
        return self

    async def __aexit__(self, *args: object) -> None:
        self.release()

    def record_success(self) -> None:
        """Mark the last API call as successful.

        Resets consecutive error count and gradually reduces the
        current delay toward ``base_delay``.
        """
        self._consecutive_errors = 0
        self._total_successes += 1

        if self._current_delay > self._config.base_delay:
            self._current_delay = max(
                self._config.base_delay,
                self._current_delay * self._config.recovery_factor,
            )
            logger.debug("Success — delay reduced to %.2fs", self._current_delay)

    def record_error(self, error_type: str = "unknown") -> None:
        """Mark the last API call as failed.

        Increments backoff.  If consecutive errors hit the threshold,
        enters cooldown mode.

        Args:
            error_type: Label for logging (e.g. "timeout", "auth").
        """
        self._consecutive_errors += 1
        self._total_errors += 1

        # Exponential backoff
        self._current_delay = min(
            self._current_delay * self._config.backoff_factor,
            self._config.max_delay,
        )
        logger.warning(
            "API error [%s] — consecutive=%d, delay=%.2fs",
            error_type,
            self._consecutive_errors,
            self._current_delay,
        )

        # Cooldown threshold
        if self._consecutive_errors >= self._config.cooldown_threshold:
            self._enter_cooldown()

    def record_rate_limit(self) -> None:
        """Mark that IG explicitly rate-limited us (429 or equivalent).

        Immediately doubles the current delay (on top of normal
        backoff) and enters cooldown if not already there.
        """
        self._consecutive_errors += 1
        self._total_errors += 1

        self._current_delay = min(
            self._current_delay * 2.0,
            self._config.max_delay,
        )
        logger.warning(
            "Rate-limit hit — delay doubled to %.2fs",
            self._current_delay,
        )
        self._enter_cooldown()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def consecutive_errors(self) -> int:
        return self._consecutive_errors

    @property
    def current_delay(self) -> float:
        return self._current_delay

    @property
    def is_cooled_down(self) -> bool:
        """True if cooldown period has elapsed (safe to proceed)."""
        if not self._in_cooldown:
            return True
        return time.monotonic() >= self._cooldown_until

    @property
    def total_calls(self) -> int:
        return self._total_calls

    @property
    def total_errors(self) -> int:
        return self._total_errors

    @property
    def total_successes(self) -> int:
        return self._total_successes

    @property
    def config(self) -> RateLimitConfig:
        return self._config

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _jittered_delay(self) -> float:
        """Return ``current_delay`` with random jitter applied."""
        jitter = self._current_delay * self._config.jitter_range * random.random()
        return self._current_delay + jitter

    def _enter_cooldown(self) -> None:
        """Enter cooldown mode — next acquire() will sleep the full duration."""
        self._in_cooldown = True
        self._cooldown_until = time.monotonic() + self._config.cooldown_duration
        logger.warning(
            "Entering cooldown for %.0fs (consecutive_errors=%d)",
            self._config.cooldown_duration,
            self._consecutive_errors,
        )
