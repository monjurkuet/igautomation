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
    sleep_hours_start: int = 18 # No sessions midnight-7am BST (6pm-1am UTC)
    sleep_hours_end: int = 1
    skip_session_probability: float = 0.05 # 5% chance to skip a session

    # Per-account cooldown (seconds) — skip accounts used within this window
    account_cooldown_seconds: int = 1800  # 30 min between sessions on same account

    # Auto-unfollow: days before unfollowing non-reciprocal follows
    unfollow_grace_days: int = 7
    max_unfollows_per_session: int = 5

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
- BD female influencers: {bd_female_count}
- By tier: {tier_breakdown}
- Sessions today: {sessions_today}
- Discovery success rates: {discovery_stats}
- Accounts needing profile refresh: {stale_accounts}
- Recent follow-back rate: {follow_back_rate}%
- Content items by status: {content_items}
- Unanalyzed accounts: {unanalyzed_count}
- Accounts needing story viewing: {story_candidates}
- Non-reciprocal follows older than 7 days: {unfollow_candidates}

Pick the next session's primary strategy and parameters. Options (ORDERED BY PRIORITY):

PRIMARY — do these most often (like a real IG user):
- feed_browsing (scroll main feed, harvest posts, like/save inline) — DEFAULT activity
- reel_browsing (swipe through Reels tab, harvest reels, engage inline) — high-value, algorithmically curated
- explore_browsing (browse trending/Explore tab) — discovery beyond feed

SECONDARY — do these when data needs attention:
- profiling (batch of accounts needing enrichment) — only if unanalyzed > 100
- content_engagement (browse, like, save, and LLM-analyze content) — if 200+ pending items
- engagement (like/follow to maintain organic appearance) — if feed browsing wasn't done recently

LOW FREQUENCY — do these occasionally:
- discovery (which strategy, what seeds/queries) — only if <200 accounts tracked
- monitoring (re-check follower counts for tracked accounts) — once every few hours
- story_viewing (watch stories from followed accounts) — passive, low-risk
- auto_unfollow (unfollow non-reciprocal follows >7 days old) — if candidates >10
- comment_engagement (leave genuine comments on engaged posts) — max 1-2/day
- own_account_monitoring (snapshot our own follower counts) — once daily

IMPORTANT: A real user checks their feed and reels throughout the day. Browsing is the DEFAULT strategy.
Don't pick discovery or profiling unless there's a clear data gap. Diversify browsing across feed/reels/explore.
If the last 2 sessions were both browsing, add one profiling or engagement session for variety.

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
            logger.info("LLM config loaded from environment (key=%s…, model=%s)",
                         self.llm_api_key[:6], self.llm_model)


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
    SessionPlan(strategy="feed_browsing", params={"max_scrolls": 15}),
    SessionPlan(strategy="reel_browsing", params={"max_reels": 20}),
    SessionPlan(strategy="feed_browsing", params={"max_scrolls": 10}),
    SessionPlan(strategy="explore_browsing", params={"max_scrolls": 10}),
    SessionPlan(strategy="reel_browsing", params={"max_reels": 15}),
    SessionPlan(strategy="feed_browsing", params={"max_scrolls": 12}),
    SessionPlan(strategy="profiling", params={"batch_size": 20}),
    SessionPlan(strategy="reel_browsing", params={"max_reels": 10}),
    SessionPlan(strategy="engagement", params={"max_likes": 5, "max_follows": 2}),
    SessionPlan(strategy="explore_browsing", params={"max_scrolls": 8}),
    SessionPlan(strategy="content_engagement", params={"max_items": 15, "analyze": True}),
    SessionPlan(strategy="feed_browsing", params={"max_scrolls": 8}),
    SessionPlan(strategy="monitoring", params={"max_accounts": 30}),
    SessionPlan(strategy="story_viewing", params={"max_stories": 8}),
    SessionPlan(strategy="reel_browsing", params={"max_reels": 12}),
    SessionPlan(strategy="discovery", params={"strategies": ["feed_browse", "discover_people"]}),
    SessionPlan(strategy="auto_unfollow", params={"max_unfollows": 5}),
    SessionPlan(strategy="profiling", params={"batch_size": 15}),
    SessionPlan(strategy="comment_engagement", params={"max_comments": 3}),
    SessionPlan(strategy="own_account_monitoring", params={}),
]
