"""Daemon configuration and strategy definitions."""

from __future__ import annotations

import yaml
from pathlib import Path
from pydantic import BaseModel
from typing import Any


class DaemonConfig(BaseModel):
    """Configuration for the IG intelligence daemon."""

    # Database
    db_path: str = "igautomation.db"

    # CDP
    cdp_port: int = 9224

    # LLM endpoint (OpenAI-compatible)
    llm_base_url: str = "https://llm.datasolved.org/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-5.4-mini"

    # Session scheduling
    max_sessions_per_day: int = 8
    sleep_hours_start: int = 2   # No sessions 2am-7am
    sleep_hours_end: int = 7
    skip_session_probability: float = 0.15  # 15% chance to skip a session

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

Pick the next session's primary strategy and parameters. Options:
- discovery (which strategy, what seeds/queries)
- profiling (batch of accounts needing enrichment)
- monitoring (re-check follower counts for tracked accounts)
- engagement (like/follow to maintain organic appearance)

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


# Default fallback plans when LLM is unavailable
FALLBACK_PLANS: list[SessionPlan] = [
    SessionPlan(strategy="discovery", params={"strategies": ["feed_browse", "discover_people"]}),
    SessionPlan(strategy="profiling", params={"batch_size": 20}),
    SessionPlan(strategy="discovery", params={"strategies": ["graphql_suggestions", "cascade"]}),
    SessionPlan(strategy="monitoring", params={"max_accounts": 30}),
    SessionPlan(strategy="engagement", params={"max_likes": 5, "max_follows": 2}),
    SessionPlan(strategy="discovery", params={"strategies": ["search", "hashtags"]}),
]
