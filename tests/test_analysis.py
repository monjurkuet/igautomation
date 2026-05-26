"""Tests for the LLM analysis module."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from igautomation.analysis.analyzer import AnalysisEngine, AnalysisResult, QUALITY_PROMPT, STRATEGY_PROMPT, TIER_PROMPT
from igautomation.db.store import AsyncDatabaseStore


# -----------------------------------------------------------------------
# AnalysisResult
# -----------------------------------------------------------------------

class TestAnalysisResult:
    def test_defaults(self):
        r = AnalysisResult(analysis_type="quality")
        assert r.analysis_type == "quality"
        assert r.summary == ""
        assert r.findings == []
        assert r.recommendations == []
        assert r.metrics == {}
        assert r.raw_response == ""
        assert r.created_at  # auto-generated

    def test_full(self):
        r = AnalysisResult(
            analysis_type="strategy",
            summary="Focus on micro-tier",
            findings=["gap in nano-tier"],
            recommendations=["add more seeds"],
            metrics={"suggested_strategy": "discovery"},
            raw_response='{"summary": "..."}',
        )
        assert len(r.findings) == 1
        assert r.metrics["suggested_strategy"] == "discovery"


# -----------------------------------------------------------------------
# AnalysisEngine — init
# -----------------------------------------------------------------------

class TestAnalysisEngineInit:
    def test_defaults(self):
        engine = AnalysisEngine()
        assert engine.db_path == "igautomation.db"
        assert engine.llm_model  # model loaded from env or defaults
        assert "datasolved" in engine.llm_base_url or engine.llm_base_url

    def test_custom(self):
        engine = AnalysisEngine(
            db_path="custom.db",
            llm_base_url="https://api.example.com/v1",
            llm_api_key="sk-test",
            llm_model="gpt-4",
        )
        assert engine.db_path == "custom.db"
        assert engine.llm_base_url == "https://api.example.com/v1"
        assert engine.llm_model == "gpt-4"


# -----------------------------------------------------------------------
# Prompt templates
# -----------------------------------------------------------------------

class TestPromptTemplates:
    def test_quality_prompt_has_placeholders(self):
        assert "{total_accounts}" in QUALITY_PROMPT
        assert "{tier_breakdown}" in QUALITY_PROMPT

    def test_strategy_prompt_has_placeholders(self):
        assert "{total_accounts}" in STRATEGY_PROMPT
        assert "{recent_results}" in STRATEGY_PROMPT

    def test_tier_prompt_has_placeholders(self):
        assert "{tier_breakdown}" in TIER_PROMPT
        assert "{follower_range}" in TIER_PROMPT

    def test_quality_prompt_format(self):
        stats = {
            "total_accounts": 100,
            "bd_female_count": 50,
            "tier_breakdown": "micro=30, mid=20",
            "stale_accounts": 10,
            "unanalyzed_count": 5,
            "sessions_today": 3,
            "discovery_stats": "graphql=60",
            "recent_sources": "graphql_suggestions(60)",
        }
        formatted = QUALITY_PROMPT.format(**stats)
        assert "100" in formatted
        assert "micro=30" in formatted


# -----------------------------------------------------------------------
# AnalysisEngine — _gather_stats
# -----------------------------------------------------------------------

class TestAnalysisEngineGatherStats:
    @pytest.mark.asyncio
    async def test_gather_stats_empty_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test_stats.db")
            db = AsyncDatabaseStore(db_path)
            await db.initialize()

            engine = AnalysisEngine(db_path=db_path)
            stats = await engine._gather_stats(db, "quality")
            assert stats["total_accounts"] == 0
            assert stats["stale_accounts"] == 0
            assert stats["sessions_today"] == 0
            await db.close()


# -----------------------------------------------------------------------
# AnalysisEngine — _parse_response
# -----------------------------------------------------------------------

class TestAnalysisEngineParseResponse:
    def test_parse_valid_json(self):
        engine = AnalysisEngine()
        raw = json.dumps({
            "summary": "Good coverage",
            "findings": ["gap in nano-tier"],
            "recommendations": ["add more seeds"],
            "metrics": {"stale_pct": 0.1},
        })
        result = engine._parse_response("quality", raw)
        assert result.summary == "Good coverage"
        assert len(result.findings) == 1
        assert result.metrics["stale_pct"] == 0.1
        assert result.raw_response == raw

    def test_parse_json_with_code_fences(self):
        engine = AnalysisEngine()
        raw = '```json\n{"summary": "OK", "findings": [], "recommendations": [], "metrics": {}}\n```'
        result = engine._parse_response("quality", raw)
        assert result.summary == "OK"

    def test_parse_invalid_json_falls_back(self):
        engine = AnalysisEngine()
        raw = "This is not JSON at all"
        result = engine._parse_response("quality", raw)
        assert "could not be parsed" in result.summary
        assert result.raw_response == raw

    def test_parse_empty_response(self):
        engine = AnalysisEngine()
        result = engine._parse_response("tier", "")
        assert "could not be parsed" in result.summary


# -----------------------------------------------------------------------
# AnalysisEngine — _call_llm (mocked)
# -----------------------------------------------------------------------

class TestAnalysisEngineCallLLM:
    @pytest.mark.asyncio
    async def test_call_llm_success(self):
        engine = AnalysisEngine(
            llm_base_url="https://fake.api/v1",
            llm_api_key="sk-test",
        )

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "choices": [{"message": {"content": '{"summary": "test"}'}}]
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = await engine._call_llm("test prompt")
            assert result == '{"summary": "test"}'

    @pytest.mark.asyncio
    async def test_call_llm_failure(self):
        engine = AnalysisEngine(
            llm_base_url="https://fake.api/v1",
            llm_api_key="sk-test",
        )
        with patch("urllib.request.urlopen", side_effect=Exception("connection error")):
            with pytest.raises(Exception, match="connection error"):
                await engine._call_llm("test prompt")


# -----------------------------------------------------------------------
# AnalysisEngine — full run (mocked LLM)
# -----------------------------------------------------------------------

class TestAnalysisEngineFullRun:
    @pytest.mark.asyncio
    async def test_run_quality_review_mocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test_analysis.db")
            engine = AnalysisEngine(db_path=db_path)

            llm_response = json.dumps({
                "summary": "Coverage is good",
                "findings": ["nano-tier under-represented"],
                "recommendations": ["use more nano seeds"],
                "metrics": {"stale_pct": 0.05, "completeness_pct": 0.85, "source_diversity": 0.7},
            })

            with patch.object(engine, "_call_llm", return_value=llm_response):
                result = await engine.run_quality_review()
                assert result.analysis_type == "quality"
                assert result.summary == "Coverage is good"
                assert len(result.findings) == 1

    @pytest.mark.asyncio
    async def test_run_strategy_optimization_mocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test_strat.db")
            engine = AnalysisEngine(db_path=db_path)

            llm_response = json.dumps({
                "summary": "Focus on profiling",
                "findings": ["many stale accounts"],
                "recommendations": ["run profiling session"],
                "metrics": {"suggested_strategy": "profiling", "priority": "high"},
            })

            with patch.object(engine, "_call_llm", return_value=llm_response):
                result = await engine.run_strategy_optimization()
                assert result.analysis_type == "strategy"
                assert result.metrics["suggested_strategy"] == "profiling"

    @pytest.mark.asyncio
    async def test_run_tier_analysis_mocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test_tier.db")
            engine = AnalysisEngine(db_path=db_path)

            llm_response = json.dumps({
                "summary": "Tier distribution is imbalanced",
                "findings": ["too many macro-tier"],
                "recommendations": ["lower macro threshold"],
                "metrics": {"tier_balance_score": 0.4, "suggested_threshold_adjustments": {}},
            })

            with patch.object(engine, "_call_llm", return_value=llm_response):
                result = await engine.run_tier_analysis()
                assert result.analysis_type == "tier"
                assert result.metrics["tier_balance_score"] == 0.4


# -----------------------------------------------------------------------
# AnalysisEngine — save_result
# -----------------------------------------------------------------------

class TestAnalysisEngineSaveResult:
    @pytest.mark.asyncio
    async def test_save_result_to_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test_save.db")
            engine = AnalysisEngine(db_path=db_path)

            result = AnalysisResult(
                analysis_type="quality",
                summary="Good data",
                findings=["finding 1"],
                recommendations=["rec 1"],
                metrics={"stale_pct": 0.1},
            )

            analysis_id = await engine.save_result(result)
            assert analysis_id > 0

            # Verify it was saved (account_id=0 for session-level)
            db = AsyncDatabaseStore(db_path)
            await db.initialize()
            cur = await db.db.execute("SELECT * FROM analysis_log WHERE id = ?", (analysis_id,))
            row = await cur.fetchone()
            assert row is not None
            assert row["analysis_type"] == "quality"
            assert row["prompt_summary"] == "Good data"
            assert row["account_id"] == 0  # session-level sentinel
            await db.close()
