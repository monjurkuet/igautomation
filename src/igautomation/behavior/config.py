"""Behavior configuration models for organic simulation.

BehaviorConfig (Pydantic v2 BaseModel) defines all tunable timing and
budget parameters.  SessionConfig (dataclass) tracks per-session usage.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# SessionConfig — mutable per-session state
# ---------------------------------------------------------------------------


@dataclass
class SessionConfig:
    """Tracks resource usage for a single automation session."""

    duration_seconds: int
    max_likes: int
    max_follows: int
    max_profile_views: int
    max_reel_views: int
    max_searches: int

    likes_used: int = 0
    follows_used: int = 0
    profile_views_used: int = 0
    reel_views_used: int = 0
    searches_used: int = 0
    started_at: float = 0.0

    # -- budget checks -------------------------------------------------------

    def can_like(self) -> bool:
        """Return True if the session still has likes available."""
        return self.likes_used < self.max_likes

    def can_follow(self) -> bool:
        """Return True if the session still has follows available."""
        return self.follows_used < self.max_follows

    def can_view_profile(self) -> bool:
        """Return True if the session still has profile views available."""
        return self.profile_views_used < self.max_profile_views

    def can_view_reel(self) -> bool:
        """Return True if the session still has reel views available."""
        return self.reel_views_used < self.max_reel_views

    def can_search(self) -> bool:
        """Return True if the session still has searches available."""
        return self.searches_used < self.max_searches

    # -- time helpers --------------------------------------------------------

    def time_remaining(self) -> float:
        """Return seconds remaining in the session (0 if unstarted)."""
        if self.started_at == 0.0:
            return float(self.duration_seconds)
        elapsed = time.monotonic() - self.started_at
        return max(0.0, self.duration_seconds - elapsed)

    def is_exhausted(self) -> bool:
        """Return True if session time is up **or** all action budgets are spent."""
        if self.time_remaining() <= 0:
            return True
        return (
            not self.can_like()
            and not self.can_follow()
            and not self.can_view_profile()
            and not self.can_view_reel()
            and not self.can_search()
        )


# ---------------------------------------------------------------------------
# BehaviorConfig — immutable configuration
# ---------------------------------------------------------------------------


class BehaviorConfig(BaseModel):
    """Tunable parameters for human-like automation behaviour.

    All fields have sensible defaults.  Use ``new_session()`` to generate a
    fresh SessionConfig with randomised duration drawn from the configured
    min/max range.
    """

    # -- action timing -------------------------------------------------------
    action_delay_min: float = 2.0
    action_delay_max: float = 8.0

    # -- scroll timing -------------------------------------------------------
    scroll_delay_min: float = 1.5
    scroll_delay_max: float = 5.0
    scroll_jitter: float = 0.3

    # -- session duration ----------------------------------------------------
    session_duration_min: int = 300
    session_duration_max: int = 1800

    # -- per-session caps ----------------------------------------------------
    likes_per_session_max: int = 20
    follows_per_session_max: int = 5
    profile_views_per_session_max: int = 30
    reel_views_per_session_max: int = 10
    searches_per_session_max: int = 8

    # -- cooldown between sessions -------------------------------------------
    session_cooldown_min: int = 600
    session_cooldown_max: int = 3600

    # -- daily caps ----------------------------------------------------------
    daily_likes_max: int = 80
    daily_follows_max: int = 20
    daily_profile_views_max: int = 100

    # -- reading dwell time --------------------------------------------------
    read_dwell_min: float = 3.0
    read_dwell_max: float = 12.0

    model_config: dict[str, Any] = {"frozen": False}

    # -- factory & random helpers --------------------------------------------

    def new_session(self) -> SessionConfig:
        """Create a new SessionConfig with randomised duration."""
        duration = random.randint(self.session_duration_min, self.session_duration_max)
        session = SessionConfig(
            duration_seconds=duration,
            max_likes=self.likes_per_session_max,
            max_follows=self.follows_per_session_max,
            max_profile_views=self.profile_views_per_session_max,
            max_reel_views=self.reel_views_per_session_max,
            max_searches=self.searches_per_session_max,
            started_at=time.monotonic(),
        )
        return session

    def action_delay(self) -> float:
        """Return a random delay between ``action_delay_min`` and ``action_delay_max``."""
        return random.uniform(self.action_delay_min, self.action_delay_max)

    def scroll_delay(self) -> float:
        """Return a random scroll delay (with jitter applied)."""
        base = random.uniform(self.scroll_delay_min, self.scroll_delay_max)
        jitter = random.uniform(-self.scroll_jitter, self.scroll_jitter)
        return max(0.0, base + jitter)

    def read_dwell(self) -> float:
        """Return a random read/dwell delay between min and max."""
        return random.uniform(self.read_dwell_min, self.read_dwell_max)

    def cooldown_seconds(self) -> int:
        """Return a random cooldown duration between sessions."""
        return random.randint(self.session_cooldown_min, self.session_cooldown_max)
