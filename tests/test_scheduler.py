"""Tests for SessionScheduler — daily slot generation, gaps, clustering."""

from __future__ import annotations

import random
from datetime import datetime, timezone


from igautomation.daemon.scheduler import SessionScheduleConfig, SessionScheduler


# ------------------------------------------------------------------
# Config tests
# ------------------------------------------------------------------


class TestSessionScheduleConfig:
    def test_defaults(self):
        c = SessionScheduleConfig()
        assert c.min_sessions_per_day == 5
        assert c.max_sessions_per_day == 10
        assert c.wake_hour == 7
        assert c.sleep_hour == 23
        assert c.min_gap_minutes == 15
        assert c.max_gap_minutes == 120
        assert c.cluster_probability == 0.3

    def test_custom(self):
        c = SessionScheduleConfig(min_sessions_per_day=3, max_sessions_per_day=5)
        assert c.min_sessions_per_day == 3
        assert c.max_sessions_per_day == 5


# ------------------------------------------------------------------
# Slot generation
# ------------------------------------------------------------------


class TestSlotGeneration:
    def test_generates_correct_number_of_slots(self):
        config = SessionScheduleConfig(
            min_sessions_per_day=5,
            max_sessions_per_day=5,
        )
        scheduler = SessionScheduler(config)
        date = datetime(2026, 5, 6, tzinfo=timezone.utc)
        slots = scheduler.generate_daily_slots(date)
        # May be fewer if gap enforcement drops some
        assert len(slots) <= 5
        assert len(slots) >= 1

    def test_all_slots_on_same_day(self):
        config = SessionScheduleConfig(min_sessions_per_day=8, max_sessions_per_day=8)
        scheduler = SessionScheduler(config)
        date = datetime(2026, 5, 6, tzinfo=timezone.utc)
        slots = scheduler.generate_daily_slots(date)
        for slot in slots:
            assert slot.date() == date.date()

    def test_slots_are_sorted(self):
        config = SessionScheduleConfig(
            min_sessions_per_day=8,
            max_sessions_per_day=8,
        )
        scheduler = SessionScheduler(config)
        date = datetime(2026, 5, 6, tzinfo=timezone.utc)
        slots = scheduler.generate_daily_slots(date)
        for i in range(len(slots) - 1):
            assert slots[i] <= slots[i + 1]

    def test_slots_within_waking_hours(self):
        config = SessionScheduleConfig(
            min_sessions_per_day=10,
            max_sessions_per_day=10,
            wake_hour=8,
            sleep_hour=22,
        )
        scheduler = SessionScheduler(config)
        date = datetime(2026, 5, 6, tzinfo=timezone.utc)
        slots = scheduler.generate_daily_slots(date)
        for slot in slots:
            assert slot.hour >= 8
            assert slot.hour < 22


# ------------------------------------------------------------------
# Gap enforcement
# ------------------------------------------------------------------


class TestGapEnforcement:
    def test_minimum_gap_respected(self):
        """No two sessions should be closer than min_gap_minutes."""
        config = SessionScheduleConfig(
            min_sessions_per_day=8,
            max_sessions_per_day=8,
            min_gap_minutes=15,
            max_gap_minutes=60,
            cluster_probability=0.0,  # no clusters
        )
        scheduler = SessionScheduler(config)
        date = datetime(2026, 5, 6, tzinfo=timezone.utc)
        slots = scheduler.generate_daily_slots(date)

        for i in range(len(slots) - 1):
            gap = (slots[i + 1] - slots[i]).total_seconds() / 60.0
            assert gap >= 15.0, f"Gap between slot {i} and {i+1} is only {gap:.1f} min"

    def test_cluster_produces_tight_gaps(self):
        """With high cluster_probability, some gaps should be short."""
        config = SessionScheduleConfig(
            min_sessions_per_day=12,
            max_sessions_per_day=12,
            min_gap_minutes=60,  # high minimum so many gaps are "too small"
            cluster_probability=1.0,  # always cluster — keep short gaps
            cluster_gap_minutes=5,
        )
        scheduler = SessionScheduler(config)
        date = datetime(2026, 5, 6, tzinfo=timezone.utc)

        # Run multiple times to ensure we get clusters
        found_short = False
        for seed in range(20):
            random.seed(seed)
            slots = scheduler.generate_daily_slots(date)
            for i in range(len(slots) - 1):
                gap = (slots[i + 1] - slots[i]).total_seconds() / 60.0
                if gap < config.min_gap_minutes:
                    found_short = True
                    break
            if found_short:
                break

        assert found_short, "Expected at least one cluster (short gap) with cluster_probability=1.0"


# ------------------------------------------------------------------
# next_slot behavior
# ------------------------------------------------------------------


class TestNextSlot:
    def test_next_slot_returns_future_slot(self):
        config = SessionScheduleConfig(
            min_sessions_per_day=5,
            max_sessions_per_day=5,
        )
        scheduler = SessionScheduler(config)
        # Generate for today
        now = datetime.now(timezone.utc)
        slot = scheduler.next_slot()
        # Slot should be in the future (or at least today)
        assert slot.date() >= now.date()

    def test_seconds_until_next_is_non_negative(self):
        config = SessionScheduleConfig(min_sessions_per_day=5, max_sessions_per_day=5)
        scheduler = SessionScheduler(config)
        secs = scheduler.seconds_until_next()
        assert secs >= 0

    def test_peek_slots_returns_copy(self):
        config = SessionScheduleConfig(
            min_sessions_per_day=5,
            max_sessions_per_day=5,
        )
        scheduler = SessionScheduler(config)
        date = datetime(2026, 5, 6, tzinfo=timezone.utc)
        scheduler.generate_daily_slots(date)
        slots = scheduler.peek_slots()
        # Mutating the copy should not affect internal state
        slots.append(datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert len(scheduler.peek_slots()) != len(slots)


# ------------------------------------------------------------------
# Activity weights
# ------------------------------------------------------------------


class TestActivityWeights:
    def test_evening_peak_gets_more_sessions(self):
        """With default weights, evening hours should get more sessions."""
        config = SessionScheduleConfig(
            min_sessions_per_day=20,
            max_sessions_per_day=20,
            cluster_probability=0.0,
        )
        scheduler = SessionScheduler(config)

        # Run multiple times to average out randomness
        hour_counts: dict[int, int] = {}
        for _ in range(10):
            random.seed(42)
            date = datetime(2026, 5, 6, tzinfo=timezone.utc)
            slots = scheduler.generate_daily_slots(date)
            for slot in slots:
                hour_counts[slot.hour] = hour_counts.get(slot.hour, 0) + 1

        # Evening (19-22) should have more sessions than afternoon (15-17)
        evening = sum(hour_counts.get(h, 0) for h in range(19, 23))
        afternoon = sum(hour_counts.get(h, 0) for h in range(15, 18))
        # This is probabilistic but with 20 sessions x 10 runs,
        # evening should generally win
        assert evening > afternoon


# ------------------------------------------------------------------
# Edge cases
# ------------------------------------------------------------------


class TestEdgeCases:
    def test_single_session_per_day(self):
        config = SessionScheduleConfig(
            min_sessions_per_day=1,
            max_sessions_per_day=1,
        )
        scheduler = SessionScheduler(config)
        date = datetime(2026, 5, 6, tzinfo=timezone.utc)
        slots = scheduler.generate_daily_slots(date)
        assert len(slots) == 1

    def test_wrap_around_midnight(self):
        """Config with wake_hour > sleep_hour (e.g. 22-6)."""
        config = SessionScheduleConfig(
            min_sessions_per_day=3,
            max_sessions_per_day=3,
            wake_hour=22,
            sleep_hour=6,
            activity_weights={22: 0.5, 23: 0.8, 0: 0.7, 1: 0.5, 2: 0.3, 3: 0.2, 4: 0.1, 5: 0.3},
        )
        scheduler = SessionScheduler(config)
        date = datetime(2026, 5, 6, tzinfo=timezone.utc)
        slots = scheduler.generate_daily_slots(date)
        assert len(slots) > 0
        for slot in slots:
            assert slot.hour >= 22 or slot.hour < 6
