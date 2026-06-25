"""AnalysisEngine — LLM-powered data quality review and strategy optimization.

After each session (or on-demand), this module queries the database, summarizes
the current state of collected data, and asks an LLM to:

1. **Quality review** — assess data completeness, flag stale/duplicate accounts,
   identify gaps in coverage (missing tiers, under-explored niches).

2. **Strategy optimization** — suggest which discovery strategies to prioritize,
   recommend seed profiles, adjust parameters.

3. **Tier analysis** — review how accounts distribute across tiers, flag
   mis-classifications, suggest re-evaluation thresholds.

The LLM calls go through the same OpenAI-compatible endpoint used by the daemon,
using the user's configured model (gpt-5.4-mini by default).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from igautomation.db.store import AsyncDatabaseStore
from igautomation.llm_config import load_llm_config

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------


class AnalysisResult(BaseModel):
    """Structured output from an LLM analysis run."""

    analysis_type: str  # "quality" | "strategy" | "tier"
    summary: str = ""
    findings: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    raw_response: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# -----------------------------------------------------------------------
# Prompt templates
# -----------------------------------------------------------------------

QUALITY_PROMPT = """You are an IG intelligence data analyst. Review the current
database state and assess data quality.

Current stats:
- Total accounts: {total_accounts}
- BD female count: {bd_female_count}
- Tier breakdown: {tier_breakdown}
- Stale accounts (>24h unchecked): {stale_accounts}
- Unanalyzed accounts: {unanalyzed_count}
- Sessions today: {sessions_today}
- Discovery strategies used: {discovery_stats}
- Top 5 recent sources: {recent_sources}

Analyze:
1. Coverage gaps — which tiers or niches are under-represented?
2. Freshness — are too many accounts stale? What %?
3. Completeness — how many accounts lack profile data?
4. Source diversity — are we over-relying on one strategy?

Respond ONLY in this JSON format:
{{
  "summary": "one-paragraph assessment",
  "findings": ["finding 1", "finding 2", ...],
  "recommendations": ["rec 1", "rec 2", ...],
  "metrics": {{"stale_pct": 0.0, "completeness_pct": 0.0, "source_diversity": 0.0}}
}}
"""

STRATEGY_PROMPT = """You are an IG intelligence strategist. Based on the current
data state, recommend the next session strategy.

Current stats:
- Total accounts: {total_accounts}
- BD female count: {bd_female_count}
- Tier breakdown: {tier_breakdown}
- Stale accounts: {stale_accounts}
- Unanalyzed accounts: {unanalyzed_count}
- Sessions today: {sessions_today}
- Discovery strategies used: {discovery_stats}
- Recent strategy results: {recent_results}

Recommend the BEST next strategy considering:
1. What would maximize new unique account discovery?
2. What would improve data quality (profiling stale accounts)?
3. What maintains organic behavior patterns?

Respond ONLY in this JSON format:
{{
  "summary": "one-paragraph strategy rationale",
  "findings": ["observation 1", "observation 2", ...],
  "recommendations": [
    "Run discovery with seed X because Y",
    "Profile stale accounts in tier Z",
    ...
  ],
  "metrics": {{"suggested_strategy": "discovery", "priority": "high", "estimated_new_accounts": 50}}
}}
"""

TIER_PROMPT = """You are an IG intelligence tier analyst. Review how discovered
accounts distribute across influencer tiers.

Tier breakdown: {tier_breakdown}
Total accounts: {total_accounts}
Followers range in DB: {follower_range}
Recent tier classifications: {recent_tiers}

Analyze:
1. Is the tier distribution realistic for the BD female influencer space?
2. Are there mis-classification patterns (e.g. too many in one tier)?
3. Should follower thresholds be adjusted?

Current tier thresholds:
- mega: >1M followers
- macro: 100K-1M
- mid: 10K-100K
- micro: 1K-10K
- nano: <1K

Respond ONLY in this JSON format:
{{
  "summary": "one-paragraph tier analysis",
  "findings": ["finding 1", ...],
  "recommendations": ["rec 1", ...],
  "metrics": {{"tier_balance_score": 0.0, "suggested_threshold_adjustments": {{}}}}
}}
"""


# -----------------------------------------------------------------------
# AnalysisEngine
# -----------------------------------------------------------------------


class AnalysisEngine:
    """Run LLM-powered analyses on collected IG data.

    Usage::

        engine = AnalysisEngine(db_path="igautomation.db")
        result = await engine.run_quality_review()
        result = await engine.run_strategy_optimization()
        result = await engine.run_tier_analysis()
    """

    def __init__(
        self,
        db_path: str = "igautomation.db",
        llm_base_url: str | None = None,
        llm_api_key: str | None = None,
        llm_model: str | None = None,
    ) -> None:
        self.db_path = db_path
        llm_cfg = load_llm_config()
        self.llm_base_url = llm_base_url or llm_cfg.base_url
        self.llm_api_key = llm_api_key or llm_cfg.api_key
        self.llm_model = llm_model or llm_cfg.model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_quality_review(self) -> AnalysisResult:
        """Assess data quality — coverage gaps, freshness, completeness."""
        return await self._run_analysis("quality", QUALITY_PROMPT)

    async def run_strategy_optimization(self) -> AnalysisResult:
        """Recommend next session strategy based on current data."""
        return await self._run_analysis("strategy", STRATEGY_PROMPT)

    async def run_tier_analysis(self) -> AnalysisResult:
        """Analyze tier distribution and suggest threshold adjustments."""
        return await self._run_analysis("tier", TIER_PROMPT)

    async def run_all(self) -> list[AnalysisResult]:
        """Run all three analyses sequentially."""
        return [
            await self.run_quality_review(),
            await self.run_strategy_optimization(),
            await self.run_tier_analysis(),
        ]

    # ------------------------------------------------------------------
    # Core
    # ------------------------------------------------------------------

    async def _run_analysis(self, analysis_type: str, prompt_template: str) -> AnalysisResult:
        """Gather stats, format prompt, call LLM, parse response."""
        db = AsyncDatabaseStore(self.db_path)
        await db.initialize()
        try:
            stats = await self._gather_stats(db, analysis_type)
            prompt = prompt_template.format(**stats)
            raw = await self._call_llm(prompt)
            return self._parse_response(analysis_type, raw)
        except Exception as e:
            logger.error("Analysis (%s) failed: %s", analysis_type, e)
            return AnalysisResult(
                analysis_type=analysis_type,
                summary=f"Analysis failed: {e}",
                findings=[],
                recommendations=[],
                raw_response="",
            )
        finally:
            await db.close()

    async def _gather_stats(self, db: AsyncDatabaseStore, analysis_type: str) -> dict[str, Any]:
        """Collect database statistics for the LLM prompt."""
        stats: dict[str, Any] = {}

        # Core counts
        cur = await db.db.execute("SELECT COUNT(*) FROM accounts")
        row = await cur.fetchone()
        stats["total_accounts"] = row[0] if row else 0

        # Tier breakdown
        cur = await db.db.execute(
            "SELECT tier, COUNT(*) as cnt FROM accounts WHERE tier IS NOT NULL GROUP BY tier"
        )
        rows = await cur.fetchall()
        stats["tier_breakdown"] = ", ".join(f"{r['tier']}={r['cnt']}" for r in rows) or "none"

        # BD female count (placeholder — no is_female column yet)
        stats["bd_female_count"] = 0

        # Stale accounts
        cur = await db.db.execute(
            """SELECT COUNT(*) FROM accounts
            WHERE last_checked_at IS NULL
            OR last_checked_at < datetime('now', '-1 day')"""
        )
        row = await cur.fetchone()
        stats["stale_accounts"] = row[0] if row else 0

        # Unanalyzed
        cur = await db.db.execute("SELECT COUNT(*) FROM accounts WHERE bio IS NULL OR bio = ''")
        row = await cur.fetchone()
        stats["unanalyzed_count"] = row[0] if row else 0

        # Discovery stats
        disc_stats = await db.get_discovery_stats()
        stats["discovery_stats"] = ", ".join(f"{k}={v}" for k, v in disc_stats.items()) or "none"

        # Recent sources (by strategy in discovery_events)
        cur = await db.db.execute(
            """SELECT strategy, COUNT(*) as cnt FROM discovery_events
            GROUP BY strategy ORDER BY cnt DESC LIMIT 5"""
        )
        rows = await cur.fetchall()
        stats["recent_sources"] = ", ".join(f"{r['strategy']}({r['cnt']})" for r in rows) or "none"

        # Sessions today
        cur = await db.db.execute(
            """SELECT COUNT(*) FROM sessions
            WHERE started_at >= datetime('now', 'start of day')"""
        )
        row = await cur.fetchone()
        stats["sessions_today"] = row[0] if row else 0

        # Strategy-specific stats
        if analysis_type == "strategy":
            cur = await db.db.execute(
                """SELECT strategy, COUNT(*) as cnt, SUM(accounts_discovered) as total_disc
                FROM sessions
                WHERE started_at >= datetime('now', '-3 days')
                GROUP BY strategy ORDER BY cnt DESC"""
            )
            rows = await cur.fetchall()
            stats["recent_results"] = (
                ", ".join(
                    f"{r['strategy']}:{r['cnt']}sessions/{r['total_disc']}accounts" for r in rows
                )
                or "none"
            )

        if analysis_type == "tier":
            # Follower range
            cur = await db.db.execute(
                "SELECT MIN(follower_count) as min_f, MAX(follower_count) as max_f FROM accounts WHERE follower_count > 0"
            )
            row = await cur.fetchone()
            stats["follower_range"] = (
                f"{row['min_f']}-{row['max_f']}" if row and row["min_f"] else "unknown"
            )

            # Recent tier classifications
            cur = await db.db.execute(
                """SELECT tier, COUNT(*) as cnt FROM accounts
                WHERE tier IS NOT NULL AND last_checked_at >= datetime('now', '-1 day')
                GROUP BY tier"""
            )
            rows = await cur.fetchall()
            stats["recent_tiers"] = ", ".join(f"{r['tier']}={r['cnt']}" for r in rows) or "none"

        return stats

    async def _call_llm(self, prompt: str) -> str:
        """Call the LLM endpoint and return the response content."""
        import urllib.request

        url = f"{self.llm_base_url.rstrip('/')}/chat/completions"
        payload = json.dumps(
            {
                "model": self.llm_model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are an IG intelligence analyst. "
                            "Respond only in valid JSON matching the requested schema."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 1000,
                "temperature": 0.7,
                "stream": False,
            }
        ).encode()

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.llm_api_key}",
        }

        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
                data = json.loads(raw)
                # Handle both streaming and non-streaming responses
                if "choices" in data and data["choices"]:
                    choice = data["choices"][0]
                    # Non-streaming: message.content
                    if "message" in choice:
                        return choice["message"]["content"]
                    # Streaming fallback (shouldn't happen with stream=False)
                    if "delta" in choice:
                        return choice["delta"].get("content", "")
                raise RuntimeError(f"Unexpected LLM response structure: {raw[:200]}")
        except Exception as e:
            logger.error("LLM API call failed: %s", e)
            raise

    def _parse_response(self, analysis_type: str, raw: str) -> AnalysisResult:
        """Parse the LLM JSON response into an AnalysisResult."""
        try:
            # Strip markdown code fences if present
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                # Remove first and last lines (fence markers)
                lines = [line for line in lines if not line.strip().startswith("```")]
                cleaned = "\n".join(lines)

            data = json.loads(cleaned)
            return AnalysisResult(
                analysis_type=analysis_type,
                summary=data.get("summary", ""),
                findings=data.get("findings", []),
                recommendations=data.get("recommendations", []),
                metrics=data.get("metrics", {}),
                raw_response=raw,
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to parse LLM response as JSON: %s", e)
            return AnalysisResult(
                analysis_type=analysis_type,
                summary="LLM response could not be parsed as structured JSON.",
                findings=[],
                recommendations=[],
                raw_response=raw,
            )

    # ------------------------------------------------------------------
    # Save results to DB
    # ------------------------------------------------------------------

    async def save_result(self, result: AnalysisResult) -> int:
        """Save an analysis result to the analysis_log table."""
        db = AsyncDatabaseStore(self.db_path)
        await db.initialize()
        try:
            analysis_id = await db.add_session_analysis(
                analysis_type=result.analysis_type,
                summary=result.summary,
                findings=json.dumps(result.findings),
                recommendations=json.dumps(result.recommendations),
                metrics=json.dumps(result.metrics),
                model_used=self.llm_model,
            )
            return analysis_id
        finally:
            await db.close()
