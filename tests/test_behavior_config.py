"""Tests for BehaviorConfig and SessionConfig."""
import time

from igautomation.behavior.config import BehaviorConfig, SessionConfig


def test_behavior_config_defaults():
    cfg = BehaviorConfig()
    assert cfg.action_delay_min == 2.0
    assert cfg.action_delay_max == 8.0
    assert cfg.scroll_delay_min == 1.5
    assert cfg.scroll_delay_max == 5.0
    assert cfg.session_duration_min == 120
    assert cfg.session_duration_max == 480
    assert cfg.likes_per_session_max == 20
    assert cfg.follows_per_session_max == 5
    assert cfg.profile_views_per_session_max == 15
    assert cfg.reel_views_per_session_max == 30
    assert cfg.searches_per_session_max == 8
    assert cfg.daily_likes_max == 80
    assert cfg.daily_follows_max == 20
    assert cfg.daily_profile_views_max == 100


def test_session_config_generation():
    cfg = BehaviorConfig()
    for _ in range(20):
        session = cfg.new_session()
        assert 120 <= session.duration_seconds <= 480
        assert session.max_likes == 20
        assert session.max_follows == 5
        assert session.max_profile_views == 15
        assert session.max_reel_views == 30
        assert session.max_searches == 8


def test_session_budget_checks():
    session = SessionConfig(
        duration_seconds=60,
        max_likes=2,
        max_follows=1,
        max_profile_views=5,
        max_reel_views=3,
        max_searches=2,
    )
    assert session.can_like()
    assert session.can_follow()
    assert session.can_view_profile()
    assert session.can_view_reel()
    assert session.can_search()

    session.likes_used = 2
    assert not session.can_like()

    session.follows_used = 1
    assert not session.can_follow()

    session.reel_views_used = 3
    assert not session.can_view_reel()


def test_session_time_remaining():
    session = SessionConfig(
        duration_seconds=10,
        max_likes=100,
        max_follows=100,
        max_profile_views=100,
        max_reel_views=100,
        max_searches=100,
    )
    # Unstarted — returns full duration
    assert session.time_remaining() == 10.0

    # Started just now — should have most time remaining
    session.started_at = time.monotonic()
    assert session.time_remaining() > 8
    assert not session.is_exhausted()

    # Started long ago — should be exhausted
    session.started_at = time.monotonic() - 20
    assert session.time_remaining() == 0.0
    assert session.is_exhausted()


def test_action_delay_range():
    cfg = BehaviorConfig(action_delay_min=1.0, action_delay_max=2.0)
    for _ in range(50):
        d = cfg.action_delay()
        assert 1.0 <= d <= 2.0


def test_cooldown_seconds_range():
    cfg = BehaviorConfig(session_cooldown_min=60, session_cooldown_max=120)
    for _ in range(50):
        c = cfg.cooldown_seconds()
        assert 60 <= c <= 120
