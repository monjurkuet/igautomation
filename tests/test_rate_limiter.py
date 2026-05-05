"""Tests for RateLimiter — exponential backoff, jitter, cooldown, recovery."""

from __future__ import annotations

import asyncio
import time

import pytest

from igautomation.behavior.rate_limiter import RateLimitConfig, RateLimiter


# ------------------------------------------------------------------
# Config tests
# ------------------------------------------------------------------


class TestRateLimitConfig:
    def test_defaults(self):
        c = RateLimitConfig()
        assert c.base_delay == 1.0
        assert c.max_delay == 300.0
        assert c.backoff_factor == 2.0
        assert c.jitter_range == 0.1
        assert c.cooldown_threshold == 5
        assert c.cooldown_duration == 60.0
        assert c.recovery_factor == 0.5
        assert c.max_concurrent == 1

    def test_custom_values(self):
        c = RateLimitConfig(base_delay=2.0, max_delay=120.0, backoff_factor=3.0)
        assert c.base_delay == 2.0
        assert c.max_delay == 120.0
        assert c.backoff_factor == 3.0


# ------------------------------------------------------------------
# Basic acquire / delay tests
# ------------------------------------------------------------------


class TestRateLimiterBasic:
    @pytest.mark.asyncio
    async def test_acquire_respects_base_delay(self):
        """Two rapid acquires should respect the base delay."""
        config = RateLimitConfig(base_delay=0.2, jitter_range=0.0)
        limiter = RateLimiter(config)

        t0 = time.monotonic()
        async with limiter:
            pass
        t1 = time.monotonic()
        async with limiter:
            pass
        t2 = time.monotonic()

        # Second call should have waited at least base_delay from first
        gap = t2 - t1
        assert gap >= 0.15  # allow small clock skew

    @pytest.mark.asyncio
    async def test_record_success_resets_errors(self):
        limiter = RateLimiter(RateLimitConfig())
        limiter.record_error("test")
        assert limiter.consecutive_errors == 1
        limiter.record_success()
        assert limiter.consecutive_errors == 0
        assert limiter.total_successes == 1

    @pytest.mark.asyncio
    async def test_counters_track(self):
        limiter = RateLimiter(RateLimitConfig(base_delay=0.01, jitter_range=0.0))
        async with limiter:
            limiter.record_success()
        async with limiter:
            limiter.record_error("fail")
        async with limiter:
            limiter.record_success()

        assert limiter.total_calls == 3
        assert limiter.total_errors == 1
        assert limiter.total_successes == 2


# ------------------------------------------------------------------
# Exponential backoff
# ------------------------------------------------------------------


class TestExponentialBackoff:
    @pytest.mark.asyncio
    async def test_backoff_increases_delay(self):
        config = RateLimitConfig(
            base_delay=0.1,
            backoff_factor=2.0,
            max_delay=50.0,
            jitter_range=0.0,
            cooldown_threshold=100,  # disable cooldown for this test
        )
        limiter = RateLimiter(config)

        limiter.record_error("e1")
        assert limiter.current_delay == pytest.approx(0.2, abs=0.01)

        limiter.record_error("e2")
        assert limiter.current_delay == pytest.approx(0.4, abs=0.01)

        limiter.record_error("e3")
        assert limiter.current_delay == pytest.approx(0.8, abs=0.01)

    @pytest.mark.asyncio
    async def test_max_delay_is_capped(self):
        config = RateLimitConfig(
            base_delay=10.0,
            backoff_factor=10.0,
            max_delay=50.0,
            jitter_range=0.0,
            cooldown_threshold=100,
        )
        limiter = RateLimiter(config)

        limiter.record_error("e1")  # 10 * 10 = 100, capped to 50
        assert limiter.current_delay == 50.0


# ------------------------------------------------------------------
# Recovery after success
# ------------------------------------------------------------------


class TestRecovery:
    @pytest.mark.asyncio
    async def test_success_reduces_delay(self):
        config = RateLimitConfig(
            base_delay=0.1,
            backoff_factor=2.0,
            recovery_factor=0.5,
            max_delay=50.0,
            jitter_range=0.0,
            cooldown_threshold=100,
        )
        limiter = RateLimiter(config)

        # Build up delay
        limiter.record_error("e1")  # 0.2
        limiter.record_error("e2")  # 0.4
        limiter.record_error("e3")  # 0.8
        assert limiter.current_delay == pytest.approx(0.8, abs=0.01)

        # Success recovers partially
        limiter.record_success()  # 0.8 * 0.5 = 0.4
        assert limiter.current_delay == pytest.approx(0.4, abs=0.01)

        limiter.record_success()  # 0.4 * 0.5 = 0.2
        assert limiter.current_delay == pytest.approx(0.2, abs=0.01)

        limiter.record_success()  # 0.2 * 0.5 = 0.1 (base_delay floor)
        assert limiter.current_delay == pytest.approx(0.1, abs=0.01)

    @pytest.mark.asyncio
    async def test_success_never_goes_below_base(self):
        config = RateLimitConfig(
            base_delay=1.0,
            recovery_factor=0.5,
            jitter_range=0.0,
            cooldown_threshold=100,
        )
        limiter = RateLimiter(config)
        limiter.record_success()
        assert limiter.current_delay >= config.base_delay


# ------------------------------------------------------------------
# Jitter
# ------------------------------------------------------------------


class TestJitter:
    def test_jittered_delay_varies(self):
        config = RateLimitConfig(base_delay=1.0, jitter_range=0.2)
        limiter = RateLimiter(config)

        delays = {limiter._jittered_delay() for _ in range(50)}
        # Should not all be identical (jitter adds randomness)
        assert len(delays) > 1

    def test_jitter_stays_in_range(self):
        config = RateLimitConfig(base_delay=1.0, jitter_range=0.3)
        limiter = RateLimiter(config)

        for _ in range(100):
            d = limiter._jittered_delay()
            # base + [0, base*jitter_range] = [1.0, 1.3]
            assert 1.0 <= d <= 1.3


# ------------------------------------------------------------------
# Cooldown
# ------------------------------------------------------------------


class TestCooldown:
    @pytest.mark.asyncio
    async def test_cooldown_activates_at_threshold(self):
        config = RateLimitConfig(
            base_delay=0.01,
            cooldown_threshold=3,
            cooldown_duration=0.1,  # short for testing
            jitter_range=0.0,
        )
        limiter = RateLimiter(config)

        limiter.record_error("e1")
        limiter.record_error("e2")
        assert not limiter._in_cooldown

        limiter.record_error("e3")
        assert limiter._in_cooldown
        assert limiter.consecutive_errors == 3

    @pytest.mark.asyncio
    async def test_cooldown_waits_on_acquire(self):
        config = RateLimitConfig(
            base_delay=0.01,
            cooldown_threshold=2,
            cooldown_duration=0.2,
            jitter_range=0.0,
        )
        limiter = RateLimiter(config)

        limiter.record_error("e1")
        limiter.record_error("e2")  # triggers cooldown
        assert limiter._in_cooldown

        t0 = time.monotonic()
        async with limiter:
            limiter.record_success()
        t1 = time.monotonic()

        # Should have waited at least the cooldown duration
        assert t1 - t0 >= 0.15

    @pytest.mark.asyncio
    async def test_is_cooled_down_property(self):
        config = RateLimitConfig(
            base_delay=0.01,
            cooldown_threshold=2,
            cooldown_duration=0.05,
            jitter_range=0.0,
        )
        limiter = RateLimiter(config)

        limiter.record_error("e1")
        limiter.record_error("e2")
        assert not limiter.is_cooled_down

        await asyncio.sleep(0.06)
        assert limiter.is_cooled_down


# ------------------------------------------------------------------
# Rate-limit response (429)
# ------------------------------------------------------------------


class TestRateLimitResponse:
    @pytest.mark.asyncio
    async def test_record_rate_limit_doubles_delay(self):
        config = RateLimitConfig(
            base_delay=1.0,
            max_delay=100.0,
            jitter_range=0.0,
            cooldown_threshold=100,  # high so cooldown doesn't trigger
        )
        limiter = RateLimiter(config)

        limiter.record_rate_limit()
        # base_delay * 2.0 = 2.0
        assert limiter.current_delay == 2.0

        limiter.record_rate_limit()
        # 2.0 * 2.0 = 4.0
        assert limiter.current_delay == 4.0

    @pytest.mark.asyncio
    async def test_rate_limit_respects_max_delay(self):
        config = RateLimitConfig(
            base_delay=50.0,
            max_delay=60.0,
            jitter_range=0.0,
            cooldown_threshold=100,
        )
        limiter = RateLimiter(config)

        limiter.record_rate_limit()  # 50 * 2 = 100, capped to 60
        assert limiter.current_delay == 60.0


# ------------------------------------------------------------------
# Context manager
# ------------------------------------------------------------------


class TestContextManager:
    @pytest.mark.asyncio
    async def test_context_manager_acquires_and_releases(self):
        config = RateLimitConfig(base_delay=0.01, max_concurrent=1, jitter_range=0.0)
        limiter = RateLimiter(config)

        async with limiter:
            # While inside, semaphore is held (value = 0)
            assert limiter._semaphore._value == 0

        # After exiting, semaphore is released
        assert limiter._semaphore._value == 1


# ------------------------------------------------------------------
# Concurrency semaphore
# ------------------------------------------------------------------


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_limit(self):
        """With max_concurrent=1, two acquires should serialise."""
        config = RateLimitConfig(base_delay=0.01, max_concurrent=1, jitter_range=0.0)
        limiter = RateLimiter(config)

        order: list[str] = []

        async def worker(label: str):
            async with limiter:
                order.append(f"{label}-start")
                await asyncio.sleep(0.05)
                order.append(f"{label}-end")

        # Launch both concurrently — only one can hold the semaphore
        await asyncio.gather(worker("A"), worker("B"))

        # They must not overlap: one completes before the other starts
        assert order.index("A-start") < order.index("A-end")
        assert order.index("B-start") < order.index("B-end")
        # A-end before B-start OR B-end before A-start
        assert (order.index("A-end") < order.index("B-start") or
                order.index("B-end") < order.index("A-start"))
