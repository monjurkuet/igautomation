"""Tests for BehaviorEngine."""
import time
from unittest.mock import MagicMock, patch

from igautomation.behavior.config import BehaviorConfig, SessionConfig
from igautomation.behavior.engine import BehaviorEngine


def _make_engine(session=None, config=None):
    cdp = MagicMock()
    cfg = config or BehaviorConfig(
        action_delay_min=0.0,
        action_delay_max=0.0,
        read_dwell_min=0.0,
        read_dwell_max=0.0,
        scroll_delay_min=0.0,
        scroll_delay_max=0.0,
        daily_likes_max=100,
        daily_follows_max=100,
        daily_profile_views_max=100,
    )
    s = session or SessionConfig(
        duration_seconds=60,
        max_likes=2,
        max_follows=1,
        max_profile_views=5,
        max_reel_views=3,
        max_searches=2,
    )
    return BehaviorEngine(cdp, cfg, s)


def test_scroll_feed_returns_links():
    engine = _make_engine()
    engine._cdp.scroll.return_value = ["user1", "user2", "user3"]
    with patch("time.sleep"):
        links = engine.scroll_feed(max_scrolls=2)
    assert links == ["user1", "user2", "user3"]


def test_view_profile_navigates_and_increments():
    engine = _make_engine()
    engine._cdp.navigate.return_value = None
    engine._cdp.evaluate.return_value = '{"meta": "", "title": "testuser"}'
    with patch("time.sleep"):
        result = engine.view_profile("testuser")
    assert result is not None
    assert engine._session.profile_views_used == 1


def test_view_profile_respects_budget():
    session = SessionConfig(
        duration_seconds=60,
        max_likes=10,
        max_follows=10,
        max_profile_views=0,
        max_reel_views=10,
        max_searches=10,
    )
    engine = _make_engine(session=session)
    with patch("time.sleep"):
        result = engine.view_profile("testuser")
    assert result is None
    assert engine._session.profile_views_used == 0


def test_like_post_navigates_and_increments():
    engine = _make_engine()
    engine._cdp.navigate.return_value = None
    engine._cdp.evaluate.return_value = "liked"
    with patch("time.sleep"):
        result = engine.like_post("https://instagram.com/p/abc")
    assert result is True
    assert engine._session.likes_used == 1


def test_like_post_respects_budget():
    session = SessionConfig(
        duration_seconds=60,
        max_likes=0,
        max_follows=10,
        max_profile_views=10,
        max_reel_views=10,
        max_searches=10,
    )
    engine = _make_engine(session=session)
    with patch("time.sleep"):
        result = engine.like_post("https://instagram.com/p/abc")
    assert result is False


def test_follow_user_navigates_and_increments():
    engine = _make_engine()
    engine._cdp.navigate.return_value = None
    engine._cdp.evaluate.return_value = "followed"
    with patch("time.sleep"):
        result = engine.follow_user("testuser")
    assert result is True
    assert engine._session.follows_used == 1


def test_search_and_browse():
    engine = _make_engine()
    graphql = MagicMock()
    graphql.search_users.return_value = [{"username": "user1"}, {"username": "user2"}]
    with patch("time.sleep"):
        results = engine.search_and_browse("bangladeshi model", graphql)
    assert len(results) == 2
    assert engine._session.searches_used == 1


def test_watch_reel_increments():
    engine = _make_engine()
    engine._cdp.navigate.return_value = None
    with patch("time.sleep"):
        result = engine.watch_reel("https://instagram.com/reel/abc")
    assert result is True
    assert engine._session.reel_views_used == 1


def test_run_session_loop_stops_on_exhausted():
    engine = _make_engine()
    call_count = 0

    def action():
        nonlocal call_count
        call_count += 1

    # Make session immediately exhausted
    engine._session.started_at = time.monotonic() - 9999
    with patch("time.sleep"):
        engine.run_session_loop([action])
    # Session is exhausted, so loop should barely run
    assert call_count <= 1


def test_daily_caps_enforced():
    config = BehaviorConfig(
        action_delay_min=0.0,
        action_delay_max=0.0,
        daily_likes_max=0,
        daily_follows_max=0,
        daily_profile_views_max=0,
    )
    engine = _make_engine(config=config)
    with patch("time.sleep"):
        assert engine.can_like() is False
        assert engine.can_follow() is False
        assert engine.can_view_profile() is False
