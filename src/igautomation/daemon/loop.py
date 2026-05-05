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
from igautomation.cdp.client import CDPClient
from igautomation.cdp.discovery import TabDiscovery
from igautomation.daemon.strategies import (
    DaemonConfig,
    FALLBACK_PLANS,
    SessionPlan,
)
from igautomation.db.store import AsyncDatabaseStore
from igautomation.graphql.client import GraphQLClient
from igautomation.scraper.analyzer import ProfileAnalyzer, _classify_tier
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
        self._running = False
        self._sessions_today = 0
        self._current_session_id: str | None = None
        self._plan_index = 0  # For cycling through fallback plans

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

            # Run one session
            await self._run_one_session()

            # Cooldown between sessions
            cfg = BehaviorConfig()
            cooldown = cfg.cooldown_seconds()
            jitter = random.randint(-60, 120)  # ±2 min jitter
            wait_time = max(60, cooldown + jitter)
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
                case "discovery":
                    result = await self._execute_discovery(
                        cdp, graphql, engine, db, plan, stats
                    )
                case "profiling":
                    result = await self._execute_profiling(
                        cdp, graphql, engine, db, plan, stats
                    )
                case "monitoring":
                    result = await self._execute_monitoring(
                        cdp, graphql, engine, db, plan, stats
                    )
                case "engagement":
                    result = await self._execute_engagement(
                        cdp, graphql, engine, db, plan, stats
                    )
                case _:
                    logger.warning("Unknown strategy: %s, falling back to discovery", plan.strategy)
                    result = await self._execute_discovery(
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

    async def _execute_discovery(
        self, cdp: CDPClient, graphql: GraphQLClient, engine: BehaviorEngine,
        db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    ) -> None:
        """Run a discovery session."""
        collector = AccountCollector(cdp, graphql, engine)
        target = plan.params.get("target_count", self.config.default_target_count)
        strategies = plan.params.get("strategies", self.config.default_strategies)
        seeds = plan.params.get("seeds", [])

        before = len(collector.accounts)
        accounts = collector.collect(
            seed_usernames=seeds,
            target_count=target,
            strategies=strategies,
        )
        new_count = len(accounts) - before

        # Save discovered accounts to DB
        for username in accounts:
            try:
                account_id = await db.upsert_account({"username": username})
                await db.add_discovery_event(
                    account_id=account_id,
                    strategy=plan.strategy,
                    source_username=seeds[0] if seeds else None,
                    query_text=json.dumps(plan.params),
                )
            except Exception as e:
                logger.debug("Failed to save %s: %s", username, e)

        stats["accounts_discovered"] = new_count
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

        analyzer = ProfileAnalyzer(cdp, graphql, engine)
        profiles = analyzer.analyze(usernames)

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

            account_id, username, old_count = row["id"], row["username"], row["follower_count"]
            profile_data = graphql.get_web_profile_info(username)
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

    async def _execute_engagement(
        self, cdp: CDPClient, graphql: GraphQLClient, engine: BehaviorEngine,
        db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    ) -> None:
        """Like/follow a few accounts to maintain organic appearance."""
        max_likes = plan.params.get("max_likes", 5)
        max_follows = plan.params.get("max_follows", 2)

        # Get some interesting accounts we haven't interacted with yet
        cur = await db.db.execute(
            """SELECT a.id, a.username FROM accounts a
               LEFT JOIN interaction_log il ON a.id = il.account_id
               WHERE il.id IS NULL AND a.tier IS NOT NULL
               ORDER BY a.follower_count DESC LIMIT 20""",
        )
        rows = await cur.fetchall()
        if not rows:
            logger.info("No accounts for engagement")
            return

        likes_done = 0
        follows_done = 0

        for row in rows:
            if engine._session.is_exhausted():
                break
            if likes_done >= max_likes and follows_done >= max_follows:
                break

            account_id, username = row["id"], row["username"]

            # Occasionally follow
            if follows_done < max_follows and engine.can_follow() and random.random() < 0.3:
                engine.follow_user(username)
                await db.log_interaction(account_id, "follow", username, self._current_session_id)
                follows_done += 1
                stats["actions_taken"] += 1

            # Occasionally view profile (organic)
            if engine.can_view_profile() and random.random() < 0.5:
                engine.view_profile(username)
                await db.log_interaction(account_id, "view_profile", username, self._current_session_id)
                stats["actions_taken"] += 1

        # Scroll feed for organic behavior
        if not engine._session.is_exhausted():
            engine.scroll_feed(max_scrolls=random.randint(2, 5))
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

            return {
                "total_accounts": total_accounts,
                "bd_female_count": 0,  # TODO: once is_female field populated
                "tier_breakdown": tier_breakdown,
                "sessions_today": self._sessions_today,
                "discovery_stats": disc_str,
                "stale_accounts": stale,
                "follow_back_rate": 0,
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_fallback_plan(self) -> SessionPlan:
        """Cycle through fallback plans when LLM is unavailable."""
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
