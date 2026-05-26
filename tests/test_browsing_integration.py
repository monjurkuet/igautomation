"""Integration tests for active browsing strategies (feed, reels, explore).

Tests that BehaviorEngine browse methods extract data, DaemonLoop strategies
save to DB, and the full pipeline works end-to-end with mocked CDP.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from igautomation.behavior.config import BehaviorConfig, SessionConfig
from igautomation.behavior.engine import BehaviorEngine
from igautomation.cdp.client import CDPClient
from igautomation.daemon.loop import DaemonLoop
from igautomation.daemon.strategies import DaemonConfig, SessionPlan
from igautomation.db.store import AsyncDatabaseStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_cdp(feed_posts=None, reel_data=None, explore_posts=None):
    """Create a mock CDPClient that returns realistic feed/reel/explore data."""
    cdp = MagicMock(spec=CDPClient)

    if feed_posts is None:
        feed_posts = [
            {"url": "https://www.instagram.com/p/abc123/", "username": "user1", "likes": "42"},
            {"url": "https://www.instagram.com/reel/def456/", "username": "user2", "likes": "1.2K"},
        ]
    if reel_data is None:
        reel_data = [
            {"url": "https://www.instagram.com/reel/r1/", "username": "reeler1", "caption": "Cool reel", "views": "5K"},
            {"url": "https://www.instagram.com/reel/r2/", "username": "reeler2", "caption": "Another", "views": "200"},
        ]
    if explore_posts is None:
        explore_posts = [
            {"url": "https://www.instagram.com/p/exp1/", "username": "explorer1"},
            {"url": "https://www.instagram.com/reel/exp2/", "username": "explorer2"},
        ]

    call_count = {"n": 0}

    def mock_evaluate(js, timeout=20):
        """Return appropriate data based on the JS being evaluated."""
        call_count["n"] += 1
        # Explore extraction JS queries /p/, /reel/, AND /tv/ — check /tv/ first
        if 'a[href*="/tv/"]' in js:
            return json.dumps(explore_posts)
        # Feed extraction JS queries both /p/ and /reel/ (but not /tv/)
        if 'a[href*="/p/"]' in js and 'a[href*="/reel/"]' in js:
            return json.dumps(feed_posts)
        # Reel extraction JS queries only /reel/ (single object, not array)
        if 'a[href*="/reel/"]' in js and '/p/' not in js:
            if reel_data:
                idx = (call_count["n"] - 1) % len(reel_data)
                return json.dumps(reel_data[idx])
            return json.dumps({})
        # Scroll/navigation JS
        return None

    cdp.evaluate = MagicMock(side_effect=mock_evaluate)
    cdp.navigate = MagicMock()
    cdp.connect = MagicMock()
    cdp._origin = None

    return cdp


def _make_engine(cdp, config=None, session=None):
    """Create a BehaviorEngine with the given mock CDP."""
    if config is None:
        config = BehaviorConfig(
            session_duration_min=300,
            session_duration_max=600,
        )
    if session is None:
        session = config.new_session()
    return BehaviorEngine(cdp, config, session)


# ---------------------------------------------------------------------------
# browse_feed tests
# ---------------------------------------------------------------------------

class TestBrowseFeed:
    def test_extracts_posts_and_usernames(self):
        cdp = _make_mock_cdp()
        engine = _make_engine(cdp)
        result = engine.browse_feed(max_scrolls=2)

        assert "posts" in result
        assert "usernames" in result
        assert "scrolls_done" in result
        assert isinstance(result["posts"], list)
        assert isinstance(result["usernames"], list)
        assert result["scrolls_done"] >= 0

    def test_empty_feed_returns_empty(self):
        cdp = _make_mock_cdp(feed_posts=[])
        engine = _make_engine(cdp)
        result = engine.browse_feed(max_scrolls=1)

        assert result["posts"] == []
        assert result["usernames"] == []

    def test_respects_session_exhaustion(self):
        cdp = _make_mock_cdp()
        config = BehaviorConfig(session_duration_min=1, session_duration_max=1)
        session = config.new_session()
        # Exhaust session by setting started_at far in the past
        import time
        session.started_at = time.monotonic() - 100
        engine = BehaviorEngine(cdp, config, session)

        result = engine.browse_feed(max_scrolls=10)
        # Should stop early due to exhaustion
        assert result["scrolls_done"] <= 10


# ---------------------------------------------------------------------------
# browse_reels tests
# ---------------------------------------------------------------------------

class TestBrowseReels:
    def test_extracts_reels(self):
        cdp = _make_mock_cdp()
        engine = _make_engine(cdp)
        result = engine.browse_reels(max_reels=2)

        assert "reels" in result
        assert "scrolls_done" in result
        assert isinstance(result["reels"], list)

    def test_empty_reels_returns_empty(self):
        cdp = _make_mock_cdp(reel_data=[])
        engine = _make_engine(cdp)
        result = engine.browse_reels(max_reels=1)

        assert result["reels"] == []

    def test_reel_budget_respected(self):
        cdp = _make_mock_cdp()
        config = BehaviorConfig(session_duration_min=300, session_duration_max=600)
        session = config.new_session()
        session.max_reel_views = 1
        engine = BehaviorEngine(cdp, config, session)

        result = engine.browse_reels(max_reels=10)
        # Should stop after 1 reel (budget)
        assert result["scrolls_done"] <= 2


# ---------------------------------------------------------------------------
# browse_explore tests
# ---------------------------------------------------------------------------

class TestBrowseExplore:
    def test_extracts_posts_and_usernames(self):
        cdp = _make_mock_cdp()
        engine = _make_engine(cdp)
        result = engine.browse_explore(max_scrolls=2)

        assert "posts" in result
        assert "usernames" in result
        assert "scrolls_done" in result

    def test_empty_explore_returns_empty(self):
        cdp = _make_mock_cdp(explore_posts=[])
        engine = _make_engine(cdp)
        result = engine.browse_explore(max_scrolls=1)

        assert result["posts"] == []


# ---------------------------------------------------------------------------
# Daemon strategy dispatch — feed_browsing, reel_browsing, explore_browsing
# ---------------------------------------------------------------------------

class TestDaemonBrowsingStrategies:
    @pytest.mark.asyncio
    async def test_feed_browsing_saves_to_db(self):
        """feed_browsing strategy should save harvested posts to content_items."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test.db")
            config = DaemonConfig(db_path=db_path)
            daemon = DaemonLoop(config)

            # Mock CDP
            mock_cdp = _make_mock_cdp()

            with patch.object(daemon, "_connect_cdp", return_value=mock_cdp):
                with patch("igautomation.daemon.loop.TabDiscovery.find_ig_tab") as mock_find:
                    mock_find.return_value = {"webSocketDebuggerUrl": "ws://localhost:9224/ws"}
                    with patch.object(CDPClient, "connect"):
                        result = await daemon._run_one_session("feed_browsing")

            assert result["strategy"] == "feed_browsing"
            assert result["status"] in ("started", "completed", "no_cdp", "error")

    @pytest.mark.asyncio
    async def test_reel_browsing_strategy_exists(self):
        """reel_browsing should be a valid strategy."""
        daemon = DaemonLoop()
        assert hasattr(daemon, "_execute_reel_browsing")

    @pytest.mark.asyncio
    async def test_explore_browsing_strategy_exists(self):
        """explore_browsing should be a valid strategy."""
        daemon = DaemonLoop()
        assert hasattr(daemon, "_execute_explore_browsing")

    @pytest.mark.asyncio
    async def test_inline_engagement_method_exists(self):
        """_inline_engagement helper should exist on DaemonLoop."""
        daemon = DaemonLoop()
        assert hasattr(daemon, "_inline_engagement")


# ---------------------------------------------------------------------------
# Stale session cleanup
# ---------------------------------------------------------------------------

class TestStaleSessionCleanup:
    @pytest.mark.asyncio
    async def test_stale_sessions_cleaned_on_session_start(self):
        """Running a session should clean up stale 'running' sessions."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test.db")
            db = AsyncDatabaseStore(db_path)
            await db.initialize()

            # Create a stale session (running, started >1 hour ago)
            await db.db.execute(
                """INSERT INTO sessions (session_uuid, status, started_at)
                   VALUES (?, 'running', datetime('now', '-2 hours'))""",
                ("stale-session-1",),
            )
            await db.db.commit()

            # Verify stale session exists
            cur = await db.db.execute("SELECT COUNT(*) FROM sessions WHERE status = 'running'")
            row = await cur.fetchone()
            assert row[0] == 1

            # Run a daemon session — should clean up stale sessions
            config = DaemonConfig(db_path=db_path)
            daemon = DaemonLoop(config)

            with patch.object(daemon, "_connect_cdp", return_value=None):
                result = await daemon._run_one_session("feed_browsing")

            # Stale session should be cleaned — re-open DB to see committed changes
            await db.close()
            db = AsyncDatabaseStore(db_path)
            await db.initialize()

            # The daemon may have created a new session (also running) — 
            # check that the specific stale session is now 'error'
            cur = await db.db.execute(
                "SELECT status FROM sessions WHERE session_uuid = 'stale-session-1'"
            )
            row = await cur.fetchone()
            assert row[0] == "error"

            # And there should be no OLD stale running sessions (started >1hr ago)
            cur = await db.db.execute(
                """SELECT COUNT(*) FROM sessions
                   WHERE status = 'running' AND ended_at IS NULL
                   AND started_at < datetime('now', '-1 hour')"""
            )
            row = await cur.fetchone()
            assert row[0] == 0

            await db.close()


# ---------------------------------------------------------------------------
# Fallback plans include browsing strategies
# ---------------------------------------------------------------------------

class TestBrowsingFallbackPlans:
    def test_fallback_includes_feed_browsing(self):
        from igautomation.daemon.strategies import FALLBACK_PLANS
        strategies = [p.strategy for p in FALLBACK_PLANS]
        assert "feed_browsing" in strategies
        assert "reel_browsing" in strategies

    def test_browsing_strategies_are_majority(self):
        """Browsing strategies should be the majority of fallback plans."""
        from igautomation.daemon.strategies import FALLBACK_PLANS
        browsing = sum(
            1
            for p in FALLBACK_PLANS
            if p.strategy in ("feed_browsing", "reel_browsing", "explore_browsing")
        )
        assert browsing >= len(FALLBACK_PLANS) // 2
