"""DaemonLoop — LLM-driven orchestrator for the IG intelligence platform.

The daemon runs sessions with cooldowns between them. Before each session,
an LLM planner reviews current data state and picks the best strategy.
After each session, results are saved to the database.

Lifecycle:
    1. Initialize DB, CDP, BehaviorEngine
    2. LLM picks strategy for session
    3. BehaviorEngine runs session
    4. Session data saved to DB
    5. LLM reviews data quality (optional)
    6. Cooldown (10-60 min with jitter)
    7. Go to 2

Usage::

    from igautomation.daemon import DaemonLoop, DaemonConfig

    config = DaemonConfig(db_path="ig.db", llm_enabled=True)
    daemon = DaemonLoop(config)
    daemon.run_forever()   # blocks, runs sessions until Ctrl+C
    daemon.run_one()       # run a single session and exit
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import signal
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from igautomation.behavior.config import BehaviorConfig
from igautomation.behavior.engine import BehaviorEngine
from igautomation.behavior.rate_limiter import RateLimiter
from igautomation.cdp.client import CDPClient
from igautomation.cdp.discovery import TabDiscovery
from igautomation.daemon.scheduler import SessionScheduler, SessionScheduleConfig
from igautomation.daemon.strategies import (
    DaemonConfig,
    FALLBACK_PLANS,
    SessionPlan,
)
from igautomation.db.store import AsyncDatabaseStore
from igautomation.graphql.client import GraphQLClient
from igautomation.scraper.analyzer import ProfileAnalyzer
from igautomation.scraper.collector import AccountCollector

logger = logging.getLogger(__name__)


class DaemonLoop:
    """LLM-driven daemon that runs organic IG intelligence sessions.

    Parameters
    ----------
    config : DaemonConfig
        Daemon configuration (DB path, LLM settings, etc.).
    """

    def __init__(self, config: DaemonConfig | None = None) -> None:
        self.config = config or DaemonConfig()
        self.config.apply_llm_config_from_env()
        self._running = False
        self._sessions_today = 0
        self._current_session_id: str | None = None
        self._plan_index = 0  # For cycling through fallback plans
        self._sessions_since_analysis = 0  # For periodic auto-analysis
        self._last_strategies: list[str] = []  # For diversity guard
        self._scheduler = SessionScheduler()  # Human-like session timing
        self._rate_limiter = RateLimiter()  # Exponential backoff for API calls

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_forever(self) -> None:
        """Run the daemon loop until interrupted."""
        self._running = True
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._run_forever_async())
        except KeyboardInterrupt:
            logger.info("Daemon interrupted by user")
        finally:
            self._running = False
            loop.close()

    def run_one(self, strategy: str | None = None) -> dict[str, Any]:
        """Run a single session and return results.

        Parameters
        ----------
        strategy : str, optional
            Force a specific strategy instead of LLM-picking.

        Returns
        -------
        dict with session stats.
        """
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(self._run_one_session(strategy))
        finally:
            loop.close()
        return result

    def stop(self) -> None:
        """Signal the daemon to stop after the current session."""
        self._running = False

    # ------------------------------------------------------------------
    # Async internals
    # ------------------------------------------------------------------

    async def _run_forever_async(self) -> None:
        """Main async loop."""
        logger.info("Daemon starting — config: db=%s, llm=%s",
                     self.config.db_path, self.config.llm_enabled)

        # Register signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)

        while self._running:
            # Check sleep hours
            if self._is_sleep_time():
                logger.info("Sleep hours — waiting 10 min")
                await asyncio.sleep(600)
                continue

            # Check daily session limit
            if self._sessions_today >= self.config.max_sessions_per_day:
                logger.info("Daily session limit reached (%d), sleeping 1h",
                            self._sessions_today)
                await asyncio.sleep(3600)
                self._sessions_today = 0
                continue

            # Random skip (simulates real-life interruptions)
            if random.random() < self.config.skip_session_probability:
                skip_time = random.randint(300, 1800)
                logger.info("Random session skip — waiting %d min", skip_time // 60)
                await asyncio.sleep(skip_time)
                continue

            # Run one session (LLM picks strategy internally)
            result = await self._run_one_session()
            strategy_used = result.get("strategy", "unknown")
            self._last_strategies.append(strategy_used)
            # Keep only last 5 for diversity tracking
            if len(self._last_strategies) > 5:
                self._last_strategies.pop(0)

            # Auto-analysis every 5 sessions
            self._sessions_since_analysis += 1
            if self._sessions_since_analysis >= 5:
                try:
                    await self._run_auto_analysis()
                except Exception as e:
                    logger.warning("Auto-analysis failed: %s", e)
                self._sessions_since_analysis = 0

            # Use scheduler for human-like session timing
            wait_time = self._scheduler.seconds_until_next()
            logger.info("Session cooldown — %d min", wait_time // 60)
            await asyncio.sleep(wait_time)

    async def _run_one_session(self, force_strategy: str | None = None) -> dict[str, Any]:
        """Execute a single daemon session.

        Returns session stats dict.
        """
        session_uuid = str(uuid.uuid4())
        self._current_session_id = session_uuid
        started_at = time.time()

        logger.info("=== Session %s starting ===", session_uuid[:8])

        # Initialize DB
        db = AsyncDatabaseStore(self.config.db_path)
        await db.initialize()

        # Clean up stale "running" sessions from previous daemon runs
        try:
            now = datetime.now(timezone.utc).isoformat()
            cursor = await db.db.execute(
                """UPDATE sessions SET status = 'error', ended_at = ?
                WHERE status = 'running' AND ended_at IS NULL
                AND started_at < datetime('now', '-1 hour')""",
                (now,),
            )
            await db.db.commit()
            if cursor.rowcount > 0:
                logger.info("Cleaned up %d stale running sessions", cursor.rowcount)
        except Exception as e:
            logger.debug("Stale session cleanup skipped: %s", e)

        try:
            await db.create_session(session_uuid)
        except Exception:
            pass  # Session table may not support this yet

        # Get LLM plan or use fallback
        if force_strategy:
            plan = SessionPlan(strategy=force_strategy)
        elif self.config.llm_enabled:
            plan = await self._get_llm_plan(db)
        else:
            plan = self._get_fallback_plan()

        logger.info("Session plan: %s — %s", plan.strategy, plan.rationale or "no rationale")

        # Set up CDP + behavior engine
        behavior_config = BehaviorConfig()
        session_config = behavior_config.new_session()

        stats: dict[str, Any] = {
            "session_uuid": session_uuid,
            "strategy": plan.strategy,
            "plan": plan.params,
            "accounts_discovered": 0,
            "accounts_profiled": 0,
            "accounts_monitored": 0,
            "actions_taken": 0,
            "duration_seconds": 0,
            "status": "started",
        }

        try:
            cdp = self._connect_cdp()
            if not cdp:
                stats["status"] = "no_cdp"
                logger.error("No CDP connection available")
                return stats

            graphql = GraphQLClient(cdp)
            engine = BehaviorEngine(cdp, behavior_config, session_config)

            # Execute strategy
            match plan.strategy:
                case "feed_browsing":
                    await self._execute_feed_browsing(
                    cdp, graphql, engine, db, plan, stats
                    )
                case "reel_browsing":
                    await self._execute_reel_browsing(
                    cdp, graphql, engine, db, plan, stats
                    )
                case "explore_browsing":
                    await self._execute_explore_browsing(
                    cdp, graphql, engine, db, plan, stats
                    )
                case "discovery":
                    await self._execute_discovery(
                        cdp, graphql, engine, db, plan, stats
                    )
                case "profiling":
                    await self._execute_profiling(
                        cdp, graphql, engine, db, plan, stats
                    )
                case "monitoring":
                    await self._execute_monitoring(
                        cdp, graphql, engine, db, plan, stats
                    )
                case "engagement":
                    await self._execute_engagement(
                        cdp, graphql, engine, db, plan, stats
                    )
                case "content_engagement":
                    await self._execute_content_engagement(
                        cdp, graphql, engine, db, plan, stats
                    )
                case _:
                    logger.warning("Unknown strategy: %s, falling back to feed_browsing", plan.strategy)
                    await self._execute_feed_browsing(
                        cdp, graphql, engine, db, plan, stats
                    )

            cdp.close()

        except Exception as e:
            logger.error("Session error: %s", e, exc_info=True)
            stats["status"] = "error"
            stats["error"] = str(e)

            # Finalize session
        stats["duration_seconds"] = round(time.time() - started_at, 1)
        if stats["status"] == "started":
            stats["status"] = "completed"

        self._sessions_today += 1

        try:
            await db.end_session(
                session_uuid,
                actions_taken=stats["actions_taken"],
                accounts_discovered=stats["accounts_discovered"],
                status=stats["status"],
            )
        except Exception:
            pass

        await db.close()

        logger.info(
            "=== Session %s done: %s | discovered=%d profiled=%d actions=%d duration=%.0fs ===",
            session_uuid[:8], stats["status"],
            stats["accounts_discovered"], stats["accounts_profiled"],
            stats["actions_taken"], stats["duration_seconds"],
        )

        return stats

    # ------------------------------------------------------------------
    # Strategy executors
    # ------------------------------------------------------------------

    async def _execute_feed_browsing(
        self, cdp: CDPClient, graphql: GraphQLClient, engine: BehaviorEngine,
        db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    ) -> None:
        """Browse the main feed like a real user — scroll, read, harvest posts, engage inline."""
        max_scrolls = plan.params.get("max_scrolls", 15)

        # Navigate to feed
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: cdp.navigate("https://www.instagram.com/", 5)
        )
        await asyncio.sleep(2)

        # Browse and extract
        result = await asyncio.get_running_loop().run_in_executor(
            None, engine.browse_feed, max_scrolls
        )

        posts = result.get("posts", [])
        usernames = result.get("usernames", [])
        stats["actions_taken"] = result.get("scrolls_done", 0)

        # Save discovered posts to content_items
        for post in posts:
            url = post.get("url", "")
            if not url:
                continue
            try:
                content_type = "reel" if "/reel/" in url else "post" if "/p/" in url else "unknown"
                await db.upsert_content_item({
                    "url": url,
                    "content_type": content_type,
                    "owner_username": post.get("username", ""),
                    "engagement_status": "pending",
                })
            except Exception as e:
                logger.debug("Failed to save feed post %s: %s", url, e)

        # Save discovered usernames to accounts
        new_accounts = 0
        for username in usernames:
            try:
                existing = await db.get_account_by_username(username)
                if not existing:
                    await db.upsert_account({"username": username})
                    new_accounts += 1
            except Exception:
                pass

        stats["accounts_discovered"] = new_accounts
        stats["posts_harvested"] = len(posts)
        logger.info("Feed browsing: %d posts, %d usernames (%d new), %d scrolls",
                     len(posts), len(usernames), new_accounts, result.get("scrolls_done", 0))

        # Inline engagement: like/save posts that meet criteria
        await self._inline_engagement(cdp, engine, db, posts, stats)

    async def _execute_reel_browsing(
        self, cdp: CDPClient, graphql: GraphQLClient, engine: BehaviorEngine,
        db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    ) -> None:
        """Swipe through reels like a real user — watch, harvest, engage inline."""
        max_reels = plan.params.get("max_reels", 20)

        result = await asyncio.get_running_loop().run_in_executor(
            None, engine.browse_reels, max_reels
        )

        reels = result.get("reels", [])
        stats["actions_taken"] = result.get("scrolls_done", 0)

        # Save discovered reels to content_items
        for reel in reels:
            url = reel.get("url", "")
            if not url:
                continue
            try:
                await db.upsert_content_item({
                    "url": url,
                    "content_type": "reel",
                    "owner_username": reel.get("username", ""),
                    "caption": reel.get("caption", ""),
                    "engagement_status": "pending",
                })
            except Exception as e:
                logger.debug("Failed to save reel %s: %s", url, e)

        # Save discovered usernames
        new_accounts = 0
        for reel in reels:
            username = reel.get("username", "")
            if not username:
                continue
            try:
                existing = await db.get_account_by_username(username)
                if not existing:
                    await db.upsert_account({"username": username})
                    new_accounts += 1
            except Exception:
                pass

        stats["accounts_discovered"] = new_accounts
        stats["reels_harvested"] = len(reels)
        logger.info("Reel browsing: %d reels, %d new accounts, %d swipes",
                     len(reels), new_accounts, result.get("scrolls_done", 0))

        # Inline engagement on watched reels
        reel_posts = [{"url": r["url"], "username": r.get("username", "")} for r in reels if r.get("url")]
        await self._inline_engagement(cdp, engine, db, reel_posts, stats)

    async def _execute_explore_browsing(
        self, cdp: CDPClient, graphql: GraphQLClient, engine: BehaviorEngine,
        db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    ) -> None:
        """Browse the Explore tab for trending content outside the user's feed."""
        max_scrolls = plan.params.get("max_scrolls", 10)

        result = await asyncio.get_running_loop().run_in_executor(
            None, engine.browse_explore, max_scrolls
        )

        posts = result.get("posts", [])
        usernames = result.get("usernames", [])
        stats["actions_taken"] = result.get("scrolls_done", 0)

        # Save discovered posts to content_items
        for post in posts:
            url = post.get("url", "")
            if not url:
                continue
            try:
                content_type = "reel" if "/reel/" in url else "post" if "/p/" in url else "unknown"
                await db.upsert_content_item({
                    "url": url,
                    "content_type": content_type,
                    "owner_username": post.get("username", ""),
                    "engagement_status": "pending",
                })
            except Exception as e:
                logger.debug("Failed to save explore post %s: %s", url, e)

        # Save discovered usernames
        new_accounts = 0
        for username in usernames:
            try:
                existing = await db.get_account_by_username(username)
                if not existing:
                    await db.upsert_account({"username": username})
                    new_accounts += 1
            except Exception:
                pass

        stats["accounts_discovered"] = new_accounts
        stats["posts_harvested"] = len(posts)
        logger.info("Explore browsing: %d posts, %d usernames (%d new), %d scrolls",
                     len(posts), len(usernames), new_accounts, result.get("scrolls_done", 0))

        await self._inline_engagement(cdp, engine, db, posts, stats)

    async def _inline_engagement(
        self, cdp: CDPClient, engine: BehaviorEngine,
        db: AsyncDatabaseStore, posts: list[dict], stats: dict,
    ) -> None:
        """Engage with harvested content inline — like/save/follow based on criteria."""
        max_inline_likes = 5
        max_inline_follows = 2
        likes_done = 0
        follows_done = 0

        for post in posts:
            if likes_done >= max_inline_likes and follows_done >= max_inline_follows:
                break
            if engine._session.is_exhausted():
                break

            url = post.get("url", "")
            username = post.get("username", "")

            # Like criteria: random ~20% chance (real users don't like everything)
            if likes_done < max_inline_likes and engine.can_like() and random.random() < 0.2:
                try:
                    liked = await asyncio.get_running_loop().run_in_executor(
                        None, engine.like_post, url
                    )
                    if liked:
                        likes_done += 1
                        stats["actions_taken"] += 1
                        try:
                            await db.update_content_engagement_status_by_url(url, "engaged")
                        except Exception:
                            pass
                except Exception:
                    pass

            # Follow criteria: if username exists and ~10% chance
            if follows_done < max_inline_follows and username and engine.can_follow() and random.random() < 0.1:
                try:
                    followed = await asyncio.get_running_loop().run_in_executor(
                        None, engine.follow_user, username
                    )
                    if followed:
                        follows_done += 1
                        stats["actions_taken"] += 1
                        account = await db.get_account_by_username(username)
                        if account:
                            await db.log_interaction(account["id"], "follow", username, self._current_session_id)
                except Exception:
                    pass

    async def _execute_discovery(
        self, cdp: CDPClient, graphql: GraphQLClient, engine: BehaviorEngine,
        db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    ) -> None:
        """Run a discovery session."""
        collector = AccountCollector(cdp, graphql, engine)
        target = plan.params.get("target_count", self.config.default_target_count)
        strategies = plan.params.get("strategies", self.config.default_strategies)
        seeds = plan.params.get("seeds", [])

        accounts = collector.collect(
            seed_usernames=seeds,
            target_count=target,
            strategies=strategies,
        )

        # Save discovered accounts to DB — track how many are truly new
        discovered_count = 0
        query_info = json.dumps({"sub_strategies": strategies, "seeds": seeds})

        for username in accounts:
            try:
                # Check if account already exists to count only truly new
                existing = await db.get_account_by_username(username)
                is_new = existing is None
                account_id = await db.upsert_account({"username": username})
                await db.add_discovery_event(
                    account_id=account_id,
                    strategy=plan.strategy,
                    source_username=seeds[0] if seeds else None,
                    query_text=query_info,
                )
                if is_new:
                    discovered_count += 1
            except Exception as e:
                logger.debug("Failed to save %s: %s", username, e)

        stats["accounts_discovered"] = discovered_count
        stats["actions_taken"] = (
            engine._session.likes_used + engine._session.follows_used +
            engine._session.profile_views_used + engine._session.searches_used
        )

    async def _execute_profiling(
        self, cdp: CDPClient, graphql: GraphQLClient, engine: BehaviorEngine,
        db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    ) -> None:
        """Run a profiling session — enrich accounts that need data."""
        batch_size = plan.params.get("batch_size", 20)

        # Get accounts needing enrichment (no bio, no follower count, etc.)
        unanalyzed = await db.get_unanalyzed_accounts(limit=batch_size)
        if not unanalyzed:
            logger.info("No accounts needing profiling")
            return

        usernames = [a["username"] for a in unanalyzed]
        logger.info("Profiling %d accounts", len(usernames))

        await self._rate_limiter.acquire()
        analyzer = ProfileAnalyzer(cdp, graphql, engine)
        loop = asyncio.get_running_loop()
        try:
            profiles = await loop.run_in_executor(None, analyzer.analyze, usernames)
            self._rate_limiter.record_success()
        except Exception:
            self._rate_limiter.record_error("profile_api")
            raise

        for profile in profiles:
            try:
                data = {
                    "username": profile.username,
                    "full_name": profile.full_name,
                    "bio": profile.bio,
                    "follower_count": profile.follower_count,
                    "following_count": profile.following_count,
                    "post_count": profile.post_count,
                    "is_private": int(profile.is_private),
                    "is_verified": int(profile.is_verified),
                    "tier": profile.tier,
                    "category": profile.category,
                }
                await db.upsert_account(data)
            except Exception as e:
                logger.debug("Failed to save profile %s: %s", profile.username, e)

        stats["accounts_profiled"] = len(profiles)
        stats["actions_taken"] = engine._session.profile_views_used

        # Compute growth status from any accumulated snapshots
        try:
            growth_counts = await db.refresh_growth_for_all()
            if growth_counts:
                logger.info("Growth status recomputed after profiling: %s", growth_counts)
        except Exception as e:
            logger.debug("Growth recomputation failed after profiling: %s", e)

    async def _execute_monitoring(
        self, cdp: CDPClient, graphql: GraphQLClient, engine: BehaviorEngine,
        db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    ) -> None:
        """Re-check follower counts for tracked accounts."""
        max_accounts = plan.params.get("max_accounts", 30)

        # Get accounts that have been checked before (need refresh)
        cur = await db.db.execute(
            """SELECT id, username, follower_count FROM accounts
               WHERE last_checked_at IS NOT NULL
               ORDER BY last_checked_at ASC LIMIT ?""",
            (max_accounts,),
        )
        rows = await cur.fetchall()
        if not rows:
            logger.info("No accounts to monitor")
            return

        for row in rows:
            if engine._session.is_exhausted():
                break

            account_id, username, _old_count = row["id"], row["username"], row["follower_count"]

            await self._rate_limiter.acquire()
            loop = asyncio.get_running_loop()
            try:
                profile_data = await loop.run_in_executor(None, graphql.get_web_profile_info, username)
                self._rate_limiter.record_success()
            except Exception:
                self._rate_limiter.record_error("profile_api")
                continue

            if not profile_data:
                continue

            new_followers = (profile_data.get("edge_followed_by", {}) or {}).get("count", 0)
            new_following = (profile_data.get("edge_follow", {}) or {}).get("count", 0)
            new_posts = (profile_data.get("edge_owner_to_timeline_media", {}) or {}).get("count", 0)

            # Update account
            await db.upsert_account({
                "username": username,
                "follower_count": new_followers,
                "following_count": new_following,
                "post_count": new_posts,
            })

            # Record snapshot
            await db.add_follower_snapshot(
                account_id=account_id,
                follower_count=new_followers,
                following_count=new_following,
                post_count=new_posts,
            )

            engine._delay()
            engine._session.profile_views_used += 1
            stats["actions_taken"] += 1

        stats["accounts_monitored"] = len(rows)

        # Compute growth status from accumulated snapshots
        try:
            growth_counts = await db.refresh_growth_for_all()
            logger.info("Growth status recomputed: %s", growth_counts)
        except Exception as e:
            logger.debug("Growth recomputation failed: %s", e)

    async def _execute_engagement(
        self, cdp: CDPClient, graphql: GraphQLClient, engine: BehaviorEngine,
        db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    ) -> None:
        """Like/follow a few accounts to maintain organic appearance."""
        max_follows = plan.params.get("max_follows", 2)
        max_profile_views = plan.params.get("max_profile_views", 5)

        # Get accounts with no prior interaction — prefer those with data
        cur = await db.db.execute(
            """SELECT a.id, a.username FROM accounts a
               LEFT JOIN interaction_log il ON a.id = il.account_id
               WHERE il.id IS NULL
               ORDER BY COALESCE(a.follower_count, 0) DESC, a.last_checked_at ASC
               LIMIT 20""",
        )
        rows = await cur.fetchall()
        if not rows:
            logger.info("No accounts for engagement")
            return

        follows_done = 0
        views_done = 0

        for row in rows:
            if engine._session.is_exhausted():
                break
            if follows_done >= max_follows and views_done >= max_profile_views:
                break

            account_id, username = row["id"], row["username"]

            # Occasionally follow
            if follows_done < max_follows and engine.can_follow() and random.random() < 0.3:
                engine.follow_user(username)
                await db.log_interaction(account_id, "follow", username, self._current_session_id)
                follows_done += 1
                stats["actions_taken"] += 1

            # Occasionally view profile (organic)
            if views_done < max_profile_views and engine.can_view_profile() and random.random() < 0.5:
                engine.view_profile(username)
                await db.log_interaction(account_id, "view_profile", username, self._current_session_id)
                views_done += 1
                stats["actions_taken"] += 1

        # Scroll feed for organic behavior
        if not engine._session.is_exhausted():
            engine.scroll_feed(max_scrolls=random.randint(2, 5))
            stats["actions_taken"] += 1

    async def _execute_content_engagement(
        self, cdp: CDPClient, graphql: GraphQLClient, engine: BehaviorEngine,
        db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    ) -> None:
        """Browse, analyze, and engage with content items (reels, posts, carousels)."""
        from igautomation.content.engager import ContentEngager
        from igautomation.content.analyzer import analyze_content_browse

        max_items = plan.params.get("max_items", 10)
        do_analyze = plan.params.get("analyze", True)

        # Get pending content items from DB
        cur = await db.db.execute(
            """SELECT id, url, content_type, shortcode FROM content_items
            WHERE engagement_status IN ('pending', 'analyzed')
            ORDER BY RANDOM() LIMIT ?""",
            (max_items,),
        )
        rows = await cur.fetchall()
        if not rows:
            logger.info("No pending content items for engagement")
            return

        engager = ContentEngager(cdp, db)

        for row in rows:
            if engine._session.is_exhausted():
                break

            item_id, url, content_type = row["id"], row["url"], row["content_type"]
            shortcode = row["shortcode"] if "shortcode" in row.keys() else ""

            # 1. Engage with the content (navigate, dwell, like, save)
            try:
                from igautomation.content.models import ContentItem, ContentType, EngagementStatus
                ct = ContentType.REEL if content_type == "Clip" else ContentType.CAROUSEL if content_type == "Carousel" else ContentType.VIDEO
                item = ContentItem(url=url, content_type=ct, shortcode=shortcode or "")
                result = engager.engage_content(item)
                stats["actions_taken"] += 1

                # Log engagement results to DB
                await engager.log_engagement(item, result, session_id=self._current_session_id)

                # Determine overall engagement status
                if result.error:
                    overall_status = "error"
                elif result.like == EngagementStatus.DONE or result.save == EngagementStatus.DONE:
                    overall_status = "engaged"
                else:
                    overall_status = "viewed"

                # Update status in DB
                await db.update_content_engagement_status(item_id, overall_status)
                logger.info("Engaged content %s → %s", url, overall_status)

            except Exception as e:
                logger.warning("Content engagement failed for %s: %s", url, e)
                await db.update_content_engagement_status(item_id, "error")
                continue

            # 2. Optionally analyze with LLM while on the page
            if do_analyze and not engine._session.is_exhausted():
                try:
                    analyzed_item = analyze_content_browse(cdp, item, dwell=3.0)
                    if analyzed_item and analyzed_item.llm_analysis:
                        await db.upsert_content_item({
                            "url": url,
                            "llm_analysis": analyzed_item.llm_analysis,
                            "category": analyzed_item.category,
                            "llm_collection_suggestion": analyzed_item.llm_collection_suggestion,
                            "is_bd_relevant": analyzed_item.is_bd_relevant,
                            "content_niche": analyzed_item.content_niche,
                        })
                        stats["actions_taken"] += 1
                        logger.info("Analyzed content %s — category=%s", shortcode, analyzed_item.category or "?")

                    # Link to collection if suggested
                    collection_name = (analyzed_item.llm_collection_suggestion or "").strip()
                    if collection_name:
                        try:
                            collection_id = await db.upsert_collection(name=collection_name)
                            await db.add_content_to_collection(item_id, collection_id)
                            logger.info("Linked content %s → collection '%s'", shortcode, collection_name)
                        except Exception as ce:
                            logger.debug("Collection link failed for %s: %s", shortcode, ce)
                except Exception as e:
                    logger.warning("Content analysis failed for %s: %s", url, e)

        # Scroll feed organically after engagement
        if not engine._session.is_exhausted():
            engine.scroll_feed(max_scrolls=random.randint(1, 3))
            stats["actions_taken"] += 1

    # ------------------------------------------------------------------
    # LLM strategy planning
    # ------------------------------------------------------------------

    async def _get_llm_plan(self, db: AsyncDatabaseStore) -> SessionPlan:
        """Ask the LLM to pick the next session strategy."""
        try:
            stats = await self._gather_stats(db)
            prompt = self.config.llm_planning_prompt.format(**stats)

            response = await self._call_llm(prompt)
            if response:
                data = json.loads(response)
                return SessionPlan(
                    strategy=data.get("strategy", "discovery"),
                    params=data.get("params", {}),
                    rationale=data.get("rationale", ""),
                )
        except Exception as e:
            logger.warning("LLM planning failed: %s — using fallback", e)

        return self._get_fallback_plan()

    async def _gather_stats(self, db: AsyncDatabaseStore) -> dict[str, Any]:
        """Gather current DB stats for the LLM prompt."""
        try:
            cur = await db.db.execute("SELECT COUNT(*) FROM accounts")
            row = await cur.fetchone()
            total_accounts = row[0] if row else 0

            cur = await db.db.execute(
                "SELECT tier, COUNT(*) as cnt FROM accounts WHERE tier IS NOT NULL GROUP BY tier"
            )
            rows = await cur.fetchall()
            tier_breakdown = ", ".join(f"{r['tier']}={r['cnt']}" for r in rows) or "none"

            discovery_stats = await db.get_discovery_stats()
            disc_str = ", ".join(f"{k}={v}" for k, v in discovery_stats.items()) or "none"

            cur = await db.db.execute(
                """SELECT COUNT(*) FROM accounts
                WHERE last_checked_at IS NULL
                OR last_checked_at < datetime('now', '-1 day')"""
        )
            row = await cur.fetchone()
            stale = row[0] if row else 0

            # Content engagement stats
            cur = await db.db.execute(
                "SELECT engagement_status, COUNT(*) as cnt FROM content_items GROUP BY engagement_status"
            )
            rows = await cur.fetchall()
            content_str = ", ".join(f"{r['engagement_status']}={r['cnt']}" for r in rows) or "none"

            return {
                "total_accounts": total_accounts,
                "bd_female_count": 0,  # Placeholder until female-relevance signals are stored in the DB
                "tier_breakdown": tier_breakdown,
                "sessions_today": self._sessions_today,
                "discovery_stats": disc_str,
                "stale_accounts": stale,
                "follow_back_rate": 0,
                "content_items": content_str,
            }
        except Exception as e:
            logger.warning("Failed to gather stats: %s", e)
            return {
                "total_accounts": 0,
                "bd_female_count": 0,
                "tier_breakdown": "none",
                "sessions_today": 0,
                "discovery_stats": "none",
                "stale_accounts": 0,
                "follow_back_rate": 0,
                "content_items": "none",
            }

    async def _call_llm(self, prompt: str) -> str | None:
        """Call the configured LLM endpoint with a prompt."""
        import urllib.request

        url = f"{self.config.llm_base_url}/chat/completions"
        payload = json.dumps({
            "model": self.config.llm_model,
            "messages": [
                {"role": "system", "content": "You are an IG intelligence analyst. Respond only in valid JSON."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 500,
            "temperature": 0.7,
        }).encode()

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.llm_api_key}",
        }

        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.warning("LLM API call failed: %s", e)
            return None

    async def _run_auto_analysis(self) -> None:
        """Run automatic quality review analysis and save to DB.

        Triggered every 5 sessions to give the daemon self-awareness
        of data quality, coverage gaps, and stale accounts.
        """
        try:
            from igautomation.analysis.analyzer import AnalysisEngine

            engine = AnalysisEngine(
                db_path=self.config.db_path,
                llm_base_url=self.config.llm_base_url,
                llm_api_key=self.config.llm_api_key,
                llm_model=self.config.llm_model,
            )
            result = await engine.run_quality_review()
            if result and result.summary:
                await engine.save_result(result)
                logger.info("Auto-analysis complete: %s (%d findings)",
                             result.summary[:60], len(result.findings))
        except Exception as e:
            logger.debug("Auto-analysis skipped: %s", e)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_fallback_plan(self) -> SessionPlan:
        """Cycle through fallback plans when LLM is unavailable.

        If the last 3+ sessions all used the same strategy, skip ahead
        to force diversity.
        """
        if len(self._last_strategies) >= 3:
            last = self._last_strategies[-1]
            prev_two = self._last_strategies[-3:]
            same_count = sum(1 for s in prev_two if s == last)
            if same_count >= 3:
                logger.info("Diversity guard: last 3 sessions all %s — cycling",
                            last)
                # Advance until we land on a different strategy
                attempts = 0
                while attempts < len(FALLBACK_PLANS):
                    self._plan_index += 1
                    candidate = FALLBACK_PLANS[self._plan_index % len(FALLBACK_PLANS)]
                    attempts += 1
                    if candidate.strategy != last:
                        return candidate

        plan = FALLBACK_PLANS[self._plan_index % len(FALLBACK_PLANS)]
        self._plan_index += 1
        return plan

    def _connect_cdp(self) -> CDPClient | None:
        """Try to connect to Chrome via CDP."""
        base = f"http://localhost:{self.config.cdp_port}"
        tab = TabDiscovery.find_ig_tab(base)
        if not tab:
            logger.error("No Instagram tab found on port %d", self.config.cdp_port)
            return None
        cdp = CDPClient()
        cdp.connect(tab["webSocketDebuggerUrl"])
        return cdp

    def _is_sleep_time(self) -> bool:
        """Return True if current hour is within sleep window."""
        hour = datetime.now(timezone.utc).hour
        if self.config.sleep_hours_start < self.config.sleep_hours_end:
            return self.config.sleep_hours_start <= hour < self.config.sleep_hours_end
        # Handle wrap-around (e.g. 22-6)
        return hour >= self.config.sleep_hours_start or hour < self.config.sleep_hours_end

    async def get_status(self) -> dict[str, Any]:
        """Return current daemon status for reporting."""
        db = AsyncDatabaseStore(self.config.db_path)
        await db.initialize()
        try:
            stats = await self._gather_stats(db)
        finally:
            await db.close()

        return {
            "running": self._running,
            "sessions_today": self._sessions_today,
            "current_session": self._current_session_id,
            **stats,
        }
