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
from igautomation.daemon.executors import (
    build_strategy_registry,
    execute_feed_browsing,
)
from igautomation.daemon.scheduler import SessionScheduler, SessionScheduleConfig
from igautomation.daemon.strategies import (
    DaemonConfig,
    FALLBACK_PLANS,
    SessionPlan,
)
from igautomation.db.store import AsyncDatabaseStore
from igautomation.graphql.client import GraphQLClient

logger = logging.getLogger(__name__)

_STRATEGY_REGISTRY = build_strategy_registry()


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
        self._sessions_date = datetime.now(timezone.utc).date().isoformat()
        self._current_session_id: str | None = None
        self._plan_index = 0
        self._sessions_since_analysis = 0
        self._last_strategies: list[str] = []
        sched_cfg = SessionScheduleConfig(
            min_sessions_per_day=self.config.schedule_min_sessions_per_day,
            max_sessions_per_day=self.config.schedule_max_sessions_per_day,
            wake_hour=self.config.schedule_wake_hour,
            sleep_hour=self.config.schedule_sleep_hour,
            min_gap_minutes=self.config.schedule_min_gap_minutes,
            max_gap_minutes=self.config.schedule_max_gap_minutes,
            cluster_probability=self.config.schedule_cluster_probability,
            cluster_gap_minutes=self.config.schedule_cluster_gap_minutes,
        )
        self._scheduler = SessionScheduler(sched_cfg)
        self._rate_limiter = RateLimiter()
        self._account_index = 0

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

        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()

        def _handle_shutdown() -> None:
            self.stop()
            shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle_shutdown)
            except (NotImplementedError, RuntimeError):
                logger.debug("Signal handler not supported on this platform")

        async def _interruptible_sleep(seconds: float) -> None:
            if shutdown_event.is_set():
                shutdown_event.clear()
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
            except asyncio.TimeoutError:
                pass

        while self._running:
            if self._is_sleep_time():
                logger.info("Sleep hours — waiting 10 min")
                await _interruptible_sleep(600)
                if not self._running:
                    break
                continue

            now_date = datetime.now(timezone.utc).date().isoformat()
            if now_date != self._sessions_date:
                self._sessions_today = 0
                self._sessions_date = now_date

            if self._sessions_today >= self.config.max_sessions_per_day:
                logger.info("Daily session limit reached (%d), sleeping 1h",
                            self._sessions_today)
                await _interruptible_sleep(3600)
                if not self._running:
                    break
                continue

            if random.random() < self.config.skip_session_probability:
                skip_time = random.randint(300, 1800)
                logger.info("Random session skip — waiting %d min", skip_time // 60)
                await _interruptible_sleep(skip_time)
                if not self._running:
                    break
                continue

            result = await self._run_one_session()
            strategy_used = result.get("strategy", "unknown")
            self._last_strategies.append(strategy_used)
            if len(self._last_strategies) > 5:
                self._last_strategies.pop(0)

            self._sessions_since_analysis += 1
            if self._sessions_since_analysis >= 5:
                try:
                    await self._run_auto_analysis()
                except Exception as e:
                    logger.warning("Auto-analysis failed: %s", e)
                self._sessions_since_analysis = 0

            wait_time = self._scheduler.seconds_until_next()
            logger.info("Session cooldown — %d min", wait_time // 60)
            await _interruptible_sleep(wait_time)

        logger.info("Daemon stopped")

    async def _run_one_session(self, force_strategy: str | None = None) -> dict[str, Any]:
        """Execute a single daemon session.

        Returns session stats dict.
        """
        session_uuid = str(uuid.uuid4())
        self._current_session_id = session_uuid
        started_at = time.time()

        logger.info("=== Session %s starting ===", session_uuid[:8])

        db = AsyncDatabaseStore(self.config.db_path)
        await db.initialize()
        try:
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

            if force_strategy:
                plan = SessionPlan(strategy=force_strategy)
            elif self.config.llm_enabled:
                plan = await self._get_llm_plan(db)
            else:
                plan = self._get_fallback_plan()

            # Enforce strategy diversity: if last 2 sessions were the same, override
            if (
                len(self._last_strategies) >= 2
                and self._last_strategies[-1] == plan.strategy
                and self._last_strategies[-2] == plan.strategy
            ):
                logger.info(
                    "Diversity override: %s repeated 2x — using fallback",
                    plan.strategy,
                )
                plan = self._get_fallback_plan()

            logger.info("Session plan: %s — %s", plan.strategy, plan.rationale or "no rationale")

            try:
                await db.create_session(session_uuid, strategy=plan.strategy)
            except Exception:
                logger.debug("Session creation skipped (table may not support it)")

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

                try:
                    graphql = GraphQLClient(cdp)
                    engine = BehaviorEngine(cdp, behavior_config, session_config)

                    executor = _STRATEGY_REGISTRY.get(plan.strategy)
                    if executor is not None:
                        await executor(
                            cdp, graphql, engine, db, plan, stats,
                            rate_limiter=self._rate_limiter,
                            current_session_id=self._current_session_id,
                            config=self.config,
                        )
                    else:
                        logger.warning("Unknown strategy: %s, falling back to feed_browsing", plan.strategy)
                        await execute_feed_browsing(
                            cdp, graphql, engine, db, plan, stats,
                            rate_limiter=self._rate_limiter,
                            current_session_id=self._current_session_id,
                            config=self.config,
                        )
                finally:
                    cdp.close()

            except Exception as e:
                logger.error("Session error: %s", e, exc_info=True)
                stats["status"] = "error"
                stats["error"] = str(e)

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
        finally:
            await db.close()

        logger.info(
            "=== Session %s done: %s | discovered=%d profiled=%d actions=%d duration=%.0fs ===",
            session_uuid[:8], stats["status"],
            stats["accounts_discovered"], stats["accounts_profiled"],
            stats["actions_taken"], stats["duration_seconds"],
        )

        return stats

    # ------------------------------------------------------------------
    # LLM strategy planning
    # ------------------------------------------------------------------

    async def _get_llm_plan(self, db: AsyncDatabaseStore) -> SessionPlan:
        """Ask the LLM to pick the next session strategy."""
        try:
            if not self.config.llm_api_key:
                return self._get_fallback_plan()
            stats = await self._gather_stats(db)
            prompt = self.config.llm_planning_prompt.format(**stats)
            response = await self._call_llm(prompt)
            if response:
                cleaned = response.strip()
                if cleaned.startswith("```"):
                    lines = cleaned.split("\n")
                    lines = [line for line in lines if not line.strip().startswith("```")]
                    cleaned = "\n".join(lines)
                data = json.loads(cleaned)
                strategy = data.get("strategy", "discovery")
                if strategy not in _STRATEGY_REGISTRY:
                    logger.warning("LLM returned unknown strategy: %s — using fallback", strategy)
                    return self._get_fallback_plan()
                return SessionPlan(
                    strategy=strategy,
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

            cur = await db.db.execute(
                "SELECT engagement_status, COUNT(*) as cnt FROM content_items GROUP BY engagement_status"
            )
            rows = await cur.fetchall()
            content_str = ", ".join(f"{r['engagement_status']}={r['cnt']}" for r in rows) or "none"

            cur = await db.db.execute(
                "SELECT COUNT(*) FROM accounts WHERE bio IS NULL OR bio = ''"
            )
            row = await cur.fetchone()
            unanalyzed_count = row[0] if row else 0

            cur = await db.db.execute(
                "SELECT COUNT(*) FROM interaction_log WHERE action_type = 'follow'"
            )
            row = await cur.fetchone()
            story_candidates = row[0] if row else 0

            cur = await db.db.execute(
                "SELECT COUNT(*) FROM interaction_log WHERE action_type = 'follow' AND performed_at < datetime('now', '-7 days')"
            )
            row = await cur.fetchone()
            unfollow_candidates = row[0] if row else 0

            return {
                "total_accounts": total_accounts,
                "tier_breakdown": tier_breakdown,
                "sessions_today": self._sessions_today,
                "discovery_stats": disc_str,
                "stale_accounts": stale,
                "content_items": content_str,
                "unanalyzed_count": unanalyzed_count,
                "story_candidates": story_candidates,
                "unfollow_candidates": unfollow_candidates,
                "last_strategy": self._last_strategies[-1] if self._last_strategies else "none",
                "last_2_strategies": ", ".join(self._last_strategies[-2:]) if len(self._last_strategies) >= 2 else "none",
            }
        except Exception as e:
            logger.warning("Failed to gather stats: %s", e)
            return {
                "total_accounts": 0,
                "tier_breakdown": "none",
                "sessions_today": 0,
                "discovery_stats": "none",
                "stale_accounts": 0,
                "content_items": "none",
                "unanalyzed_count": 0,
                "story_candidates": 0,
                "unfollow_candidates": 0,
                "last_strategy": "none",
                "last_2_strategies": "none",
            }

    async def _call_llm(self, prompt: str) -> str | None:
        """Call the configured LLM endpoint with a prompt."""
        import urllib.request

        base = self.config.llm_base_url.rstrip("/")
        url = f"{base}/chat/completions"
        payload = json.dumps({
            "model": self.config.llm_model,
            "messages": [
                {"role": "system", "content": "You are an IG intelligence analyst. Respond only in valid JSON."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 500,
            "temperature": 0.7,
            "stream": False,
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

    def _connect_cdp(self):
        """Try to connect to Chrome via CDP.

        If no Instagram tab is found, auto-navigates an existing
        tab to instagram.com (auto-recovery from browser restarts).
        """
        ports = self.config.ports
        if ports and len(ports) > 0:
            idx = self._account_index % len(ports)
            port = ports[idx]
            self._account_index += 1
            base = f"http://localhost:{port}"
        else:
            port = self.config.cdp_port
            base = f"http://localhost:{port}"
        tab = TabDiscovery.find_ig_tab(base)
        if not tab:
            # Auto-recovery: navigate an existing tab to Instagram
            logger.info("No IG tab on port %d — attempting auto-navigate", port)
            tab = self._auto_navigate_to_ig(base, port)
            if not tab:
                logger.error("No Instagram tab found on port %d (auto-navigate failed)", port)
                return None
        cdp = CDPClient()
        cdp.connect(tab["webSocketDebuggerUrl"])
        return cdp

    def _auto_navigate_to_ig(self, base: str, port: int) -> dict | None:
        """Navigate an existing browser tab to instagram.com.

        Returns the IG tab dict on success, None on failure.
        """
        import urllib.request as _urllib_request
        try:
            resp = _urllib_request.urlopen(f"{base}/json/list", timeout=5)
            all_tabs = json.loads(resp.read())
            # Pick a real http tab (not extensions, blobs, devtools)
            real_tabs = [
                t for t in all_tabs
                if t.get("url", "").startswith("http")
                and "chrome-extension" not in t.get("url", "")
                and "blob:" not in t.get("url", "")
                and "devtools" not in t.get("url", "")
            ]
            if not real_tabs:
                logger.warning("No real tabs on port %d for auto-navigate", port)
                return None
            target = real_tabs[0]
            cdp = CDPClient()
            cdp.connect(target["webSocketDebuggerUrl"])
            cdp.navigate("https://www.instagram.com/", wait=6)
            login = cdp.evaluate(
                'document.querySelector("svg[aria-label=Home]") ? "LOGGED_IN" : "NOT_LOGGED_IN"'
            )
            if login == "LOGGED_IN":
                logger.info("Auto-navigated to IG on port %d — logged in", port)
                # Re-discover the tab (URL changed after navigate)
                ig_tab = TabDiscovery.find_ig_tab(base)
                return ig_tab
            else:
                logger.warning("Auto-navigated to IG on port %d — NOT logged in", port)
                return None
        except Exception as e:
            logger.warning("Auto-navigate to IG failed on port %d: %s", port, e)
            return None

    def _is_sleep_time(self) -> bool:
        """Return True if current hour is within sleep window."""
        hour = datetime.now(timezone.utc).hour
        if self.config.sleep_hours_start < self.config.sleep_hours_end:
            return self.config.sleep_hours_start <= hour < self.config.sleep_hours_end
        return hour >= self.config.sleep_hours_start or hour < self.config.sleep_hours_end

    async def get_status(self) -> dict[str, Any]:
        """Return current daemon status for reporting."""
        db = AsyncDatabaseStore(self.config.db_path)
        await db.initialize()
        try:
            stats = await self._gather_stats(db)
        finally:
            await db.close()
        slots_iso: list[str] = []
        try:
            slots_iso = [s.isoformat() for s in self._scheduler.peek_slots()]
        except Exception:
            pass
        return {
            "running": self._running,
            "sessions_today": self._sessions_today,
            "current_session": self._current_session_id,
            "upcoming_slots": slots_iso,
            **stats,
        }