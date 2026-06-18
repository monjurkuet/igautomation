"""Daemon configuration and strategy definitions."""

from __future__ import annotations

import logging
import yaml
from pathlib import Path
from pydantic import BaseModel
from typing import Any

from igautomation.llm_config import load_llm_config

logger = logging.getLogger(__name__)


class DaemonConfig(BaseModel):
    """Configuration for the IG intelligence daemon."""

    # Database
    db_path: str = "igautomation.db"

    # CDP — multi-port support
    cdp_port: int = 9224  # Legacy: used when ports is empty
    ports: list[int] = [9222, 9224, 9225]  # Active CDP ports for account rotation
    account_rotation: str = "round_robin"  # round_robin | least_recently_used | random

    # Per-account strategy preferences (port → preferred strategies)
    account_strategies: dict[int, list[str]] = {}

    # LLM endpoint (OpenAI-compatible)
    llm_base_url: str = "https://llm.datasolved.org/v1"
    llm_api_key: str = ""
    llm_model: str = "gemini-2.5-flash-lite"

    # Session scheduling
    max_sessions_per_day: int = 16
    # Active hours: 01:00-18:00 UTC = 07:00-00:00 BDT
    # (sleep_hours_start=18 means daemon sleeps 18:00-01:00 UTC)
    sleep_hours_start: int = 18  # No sessions 18:00-01:00 UTC (midnight-7am BDT)
    sleep_hours_end: int = 1
    skip_session_probability: float = 0.05  # 5% chance to skip a session

    # Scheduler config — must align with active hours (01:00-18:00 UTC)
    schedule_min_sessions_per_day: int = 20
    schedule_max_sessions_per_day: int = 40
    schedule_wake_hour: int = 1   # UTC — aligned with sleep_hours_end
    schedule_sleep_hour: int = 18  # UTC — aligned with sleep_hours_start
    schedule_min_gap_minutes: int = 5
    schedule_max_gap_minutes: int = 30
    schedule_cluster_probability: float = 0.4
    schedule_cluster_gap_minutes: int = 3

    # Per-account cooldown (seconds) — skip accounts used within this window
    account_cooldown_seconds: int = 600  # 10 min between sessions on same account

    # Auto-unfollow: days before unfollowing non-reciprocal follows
    unfollow_grace_days: int = 7
    max_unfollows_per_session: int = 5

    # Comment automation (disabled by default)
    comment_enabled: bool = False

    # Discovery defaults
    default_target_count: int = 100
    default_strategies: list[str] = [
        "existing_tabs",
        "feed_browse",
        "discover_people",
        "shoutout_pages",
        "graphql_suggestions",
        "search",
        "hashtags",
        "cascade",
    ]

    # LLM strategy planning
    llm_enabled: bool = True
    llm_planning_prompt: str = """You are an Instagram intelligence analyst. Current stats:
- Total accounts in DB: {total_accounts}
- By tier: {tier_breakdown}
- Sessions today: {sessions_today}
- Discovery success rates: {discovery_stats}
- Accounts needing profile refresh: {stale_accounts}
- Content items by status: {content_items}
- Unanalyzed accounts (no bio/profile data): {unanalyzed_count}
- Accounts needing story viewing: {story_candidates}
- Non-reciprocal follows older than 7 days: {unfollow_candidates}
- Last session strategy: {last_strategy}
- Last 2 session strategies: {last_2_strategies}

Pick the next session's primary strategy and parameters. DECISION PRIORITY:

1. If the last 3 strategies are ALL non-browsing (profiling, discovery, monitoring, engagement, content_engagement) → pick a browsing strategy (feed_browsing, reel_browsing, or explore_browsing) to collect fresh content.

2. If unanalyzed accounts > 50 AND last session was NOT profiling AND at least 2 browsing sessions have happened since last profiling → profiling (enrich profiles: follower count, bio, tier)

3. If stale_accounts > 100 AND last session was NOT monitoring AND at least 1 browsing session since last monitoring → monitoring (refresh existing profile data)

4. If pending content > 200 AND last session was NOT content_engagement → content_engagement (analyze + engage)

5. If accounts with no interactions exist AND last session was NOT engagement → engagement (like/follow to build organic presence)

6. If total accounts < 300 AND last session was NOT discovery → discovery (find new accounts to track)

7. Otherwise → browse (rotate between feed_browsing, reel_browsing, explore_browsing)

IMPORTANT: Browsing sessions (feed_browsing, reel_browsing, explore_browsing) are the PRIMARY data pipeline — they discover new content and accounts. Without them, the DB doesn't grow. Ensure at least 1 browsing session for every 2 non-browsing sessions.
NEVER repeat the same strategy 3 times in a row. If last 2 sessions were the same strategy, pick a different one.
Profile enrichment is important but not at the expense of all other data pipelines.
After 2 profiling sessions, always pick a non-profiling strategy.

Respond in JSON: {{"strategy": "...", "params": {{...}}, "rationale": "..."}}"""

    model_config: dict[str, Any] = {"frozen": False}

    @classmethod
    def from_yaml(cls, path: str | Path) -> DaemonConfig:
        """Load config from a YAML file."""
        p = Path(path)
        if not p.exists():
            return cls()
        with open(p) as f:
            data = yaml.safe_load(f) or {}
        return cls(**data)

    def to_yaml(self, path: str | Path) -> None:
        """Save config to a YAML file."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            yaml.dump(self.model_dump(), f, default_flow_style=False)

    def apply_llm_config_from_env(self) -> None:
        """Load LLM credentials from environment / .env file if not already set.

        Called by DaemonLoop.__init__() so all startup paths (CLI, python -m,
        direct instantiation) benefit from centralized LLM config loading.
        """
        if self.llm_api_key:
            return  # Already configured explicitly

        llm_cfg = load_llm_config()
        if llm_cfg.api_key:
            self.llm_api_key = llm_cfg.api_key
        if llm_cfg.base_url and llm_cfg.base_url != self.llm_base_url:
            self.llm_base_url = llm_cfg.base_url
        if llm_cfg.model:
            self.llm_model = llm_cfg.model

        if self.llm_api_key:
            logger.info("LLM config loaded from environment (key=present, model=%s)",
                         self.llm_model)


# -----------------------------------------------------------------------
# Strategy types
# -----------------------------------------------------------------------

class SessionPlan:
    """A plan for a single daemon session, possibly LLM-generated."""

    def __init__(
        self,
        strategy: str = "discovery",
        params: dict[str, Any] | None = None,
        rationale: str = "",
    ) -> None:
        self.strategy = strategy
        self.params = params or {}
        self.rationale = rationale

    def __repr__(self) -> str:
        return f"SessionPlan(strategy={self.strategy!r}, params={self.params!r})"


# Default fallback plans — browsing-first, with secondary strategies mixed in
FALLBACK_PLANS: list[SessionPlan] = [
    SessionPlan(strategy="profiling", params={"batch_size": 20}),
    SessionPlan(strategy="reel_browsing", params={"max_reels": 20}),
    SessionPlan(strategy="monitoring", params={"max_accounts": 30}),
    SessionPlan(strategy="explore_browsing", params={"max_scrolls": 10}),
    SessionPlan(strategy="engagement", params={"max_likes": 5, "max_follows": 2}),
    SessionPlan(strategy="content_engagement", params={"max_items": 10, "analyze": True}),
    SessionPlan(strategy="feed_browsing", params={"max_scrolls": 15}),
    SessionPlan(strategy="own_account_monitoring", params={}),
    SessionPlan(strategy="profiling", params={"batch_size": 15}),
    SessionPlan(strategy="reel_browsing", params={"max_reels": 15}),
    SessionPlan(strategy="monitoring", params={"max_accounts": 20}),
    SessionPlan(strategy="discovery", params={"strategies": ["feed_browse", "discover_people"]}),
    SessionPlan(strategy="explore_browsing", params={"max_scrolls": 8}),
    SessionPlan(strategy="content_engagement", params={"max_items": 5, "analyze": False}),
    SessionPlan(strategy="feed_browsing", params={"max_scrolls": 10}),
    SessionPlan(strategy="engagement", params={"max_likes": 3, "max_follows": 1}),
    SessionPlan(strategy="reel_browsing", params={"max_reels": 12}),
    SessionPlan(strategy="profiling", params={"batch_size": 10}),
    SessionPlan(strategy="comment_engagement", params={"max_comments": 3}),
    SessionPlan(strategy="auto_unfollow", params={"max_unfollows": 5}),
]
