"""Session scheduler — generates human-like, unpredictable session patterns.

Instead of fixed-interval sessions, the scheduler produces a daily plan
where sessions are scattered across waking hours with natural clustering
(morning burst, afternoon check, evening scroll) and random gaps.

Usage::

    scheduler = SessionScheduler(SessionScheduleConfig())
    today_slots = scheduler.generate_daily_slots()
    # -> [datetime(2026, 5, 6, 8, 23), datetime(2026, 5, 6, 9, 7), ...]

    # In the daemon loop:
    next_slot = scheduler.next_slot()
    wait = (next_slot - datetime.now(timezone.utc)).total_seconds()
    await asyncio.sleep(wait)
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------


class SessionScheduleConfig(BaseModel):
    """Tunable knobs for the session scheduler."""

    # How many sessions per day (range — actual count is randomized)
    min_sessions_per_day: int = Field(5, ge=1, description="Minimum sessions per day")
    max_sessions_per_day: int = Field(10, ge=1, description="Maximum sessions per day")

    # Waking hours (UTC) — sessions only happen in this window
    wake_hour: int = Field(7, ge=0, le=23, description="Hour (UTC) when sessions start")
    sleep_hour: int = Field(23, ge=0, le=23, description="Hour (UTC) when sessions stop")

    # Inter-session gap (minutes)
    min_gap_minutes: int = Field(15, ge=1, description="Minimum minutes between sessions")
    max_gap_minutes: int = Field(120, ge=1, description="Maximum minutes between sessions")

    # Cluster probability: chance that the next session is close to the
    # previous one (simulates "checking phone repeatedly")
    cluster_probability: float = Field(
        0.3,
        ge=0.0,
        le=1.0,
        description="Probability of a tight cluster (short gap after previous session)",
    )
    cluster_gap_minutes: int = Field(
        5, ge=1, description="Minutes between sessions in a cluster"
    )

    # Activity profile weights by hour (UTC) — higher weight = more likely
    # to have a session in that hour. 7am-10am = morning check,
    # 12pm-2pm = lunch, 7pm-11pm = evening scrolling.
    activity_weights: dict[int, float] = Field(
        default_factory=lambda: {
            7: 0.6, 8: 0.8, 9: 0.9, 10: 0.7,   # morning
            11: 0.4, 12: 0.7, 13: 0.8, 14: 0.5,  # midday
            15: 0.3, 16: 0.4, 17: 0.5, 18: 0.6,  # afternoon
            19: 0.8, 20: 0.9, 21: 1.0, 22: 0.9,  # evening peak
        },
        description="Hour (UTC) → activity weight (higher = more sessions in that hour)",
    )


# ------------------------------------------------------------------
# Scheduler
# ------------------------------------------------------------------


class SessionScheduler:
    """Generates human-like daily session schedules with randomization.

    The key insight: real humans don't space their IG sessions evenly.
    They have bursts (morning check, evening scroll) with long gaps
    in between.  This scheduler produces that pattern.
    """

    def __init__(self, config: SessionScheduleConfig | None = None) -> None:
        self._config = config or SessionScheduleConfig()
        self._slots: list[datetime] = []
        self._slot_index: int = 0
        self._generated_date: str = ""  # ISO date string

    @property
    def config(self) -> SessionScheduleConfig:
        return self._config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_daily_slots(self, date: datetime | None = None) -> list[datetime]:
        """Generate randomized session time slots for a given date.

        Args:
            date: The date to generate slots for (defaults to today UTC).

        Returns:
            Sorted list of datetime objects (UTC) when sessions should run.
        """
        target = (date or datetime.now(timezone.utc)).replace(
            tzinfo=timezone.utc,
        )
        day = target.date()
        day_str = day.isoformat()

        # Reset if new day
        if day_str != self._generated_date:
            self._slot_index = 0
            self._generated_date = day_str

        num_sessions = random.randint(
            self._config.min_sessions_per_day,
            self._config.max_sessions_per_day,
        )

        # Build weighted hour distribution
        hours = list(range(self._config.wake_hour, self._config.sleep_hour))
        weights = [self._config.activity_weights.get(h, 0.5) for h in hours]

        # Assign sessions to hours using weighted sampling
        hour_assignments: list[int] = []
        for _ in range(num_sessions):
            if not hours:
                break
            # Weighted random choice
            total = sum(weights)
            r = random.random() * total
            cumulative = 0.0
            chosen = hours[0]
            for h, w in zip(hours, weights):
                cumulative += w
                if r <= cumulative:
                    chosen = h
                    break
            hour_assignments.append(chosen)

        # Within each hour, randomize the minute
        slots: list[datetime] = []
        for hour in sorted(hour_assignments):
            minute = random.randint(0, 59)
            second = random.randint(0, 59)
            slot = datetime(
                day.year, day.month, day.day,
                hour, minute, second,
                tzinfo=timezone.utc,
            )
            slots.append(slot)

        # Sort and enforce minimum gaps
        slots = self._enforce_gaps(slots)

        self._slots = slots
        self._slot_index = 0
        self._generated_date = day_str

        logger.info(
            "Generated %d session slots for %s",
            len(slots),
            day_str,
        )
        return slots

    def next_slot(self) -> datetime:
        """Return the next session slot, generating a new day if needed.

        If all slots for today are exhausted, generates tomorrow's slots.
        """
        now = datetime.now(timezone.utc)
        today_str = now.date().isoformat()

        # Generate if needed
        if today_str != self._generated_date or not self._slots:
            self.generate_daily_slots(now)

        # Find next slot that hasn't passed
        while self._slot_index < len(self._slots):
            slot = self._slots[self._slot_index]
            self._slot_index += 1
            if slot > now:
                return slot

        # All today's slots passed — generate for tomorrow
        tomorrow = now + timedelta(days=1)
        self.generate_daily_slots(tomorrow)
        self._slot_index = 0
        return self._slots[0]

    def seconds_until_next(self) -> float:
        """Convenience: seconds from now until the next slot."""
        slot = self.next_slot()
        delta = slot - datetime.now(timezone.utc)
        return max(0.0, delta.total_seconds())

    def peek_slots(self) -> list[datetime]:
        """Return the current day's generated slots (read-only copy)."""
        return list(self._slots)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _enforce_gaps(self, slots: list[datetime]) -> list[datetime]:
        """Enforce gaps between sessions with cluster logic.

        Clusters: with ``cluster_probability``, a short gap is allowed
        (simulates checking phone repeatedly). Otherwise, the minimum
        gap is enforced by pushing the slot forward.
        """
        if not slots:
            return slots

        # Day boundary — no session past sleep_hour
        day = slots[0].date()
        sleep_time = datetime(
            day.year, day.month, day.day,
            self._config.sleep_hour, 0, 0,
            tzinfo=timezone.utc,
        )
        # Handle wrap-around: if sleep_hour < wake_hour, sleep is next day
        if self._config.sleep_hour <= self._config.wake_hour:
            sleep_time += timedelta(days=1)

        result: list[datetime] = [slots[0]]

        for slot in slots[1:]:
            prev = result[-1]
            gap = (slot - prev).total_seconds() / 60.0  # minutes

            if gap < self._config.min_gap_minutes:
                # Decide: is this a cluster (short gap OK) or not?
                if random.random() < self._config.cluster_probability:
                    # Cluster: allow the short gap as-is
                    if slot < sleep_time:
                        result.append(slot)
                else:
                    # Not a cluster — push forward to respect min_gap
                    new_gap = random.uniform(
                        self._config.min_gap_minutes,
                        self._config.max_gap_minutes,
                    )
                    new_slot = prev + timedelta(minutes=new_gap)
                    if new_slot < sleep_time:
                        result.append(new_slot)
                    elif slot < sleep_time:
                        result.append(slot)
            else:
                if slot < sleep_time:
                    result.append(slot)

        # Cluster insertion can break sort order — re-sort
        result.sort()
        return result

    def _decide_gap(self) -> float:
        """Decide the gap (in minutes) until the next session.

        Uses cluster_probability: if we're in a cluster, the gap is
        short; otherwise, it's drawn from the normal range.
        """
        if random.random() < self._config.cluster_probability:
            # Cluster: tight gap
            gap = self._config.cluster_gap_minutes + random.uniform(0, 5)
            logger.debug("Cluster gap: %.1f min", gap)
        else:
            # Normal gap
            gap = random.uniform(
                self._config.min_gap_minutes,
                self._config.max_gap_minutes,
            )
            logger.debug("Normal gap: %.1f min", gap)
        return gap
