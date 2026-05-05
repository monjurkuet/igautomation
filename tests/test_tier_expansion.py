"""Tests for the expanded tier system and growth status computation."""

import pytest
from datetime import datetime, timedelta, timezone


class TestExpandedTiers:
    """Test the 6-tier classification system."""

    def test_mega_tier(self):
        from igautomation.scraper.analyzer import _classify_tier
        assert _classify_tier(1_500_000) == "mega"
        assert _classify_tier(1_000_000) == "mega"

    def test_macro_tier(self):
        from igautomation.scraper.analyzer import _classify_tier
        assert _classify_tier(500_000) == "macro"
        assert _classify_tier(100_000) == "macro"
        assert _classify_tier(99_999) != "macro"

    def test_mid_tier(self):
        from igautomation.scraper.analyzer import _classify_tier
        assert _classify_tier(50_000) == "mid"
        assert _classify_tier(25_000) == "mid"
        assert _classify_tier(24_999) != "mid"

    def test_micro_tier(self):
        from igautomation.scraper.analyzer import _classify_tier
        assert _classify_tier(10_000) == "micro"
        assert _classify_tier(5_000) == "micro"
        assert _classify_tier(4_999) != "micro"

    def test_nano_tier(self):
        from igautomation.scraper.analyzer import _classify_tier
        assert _classify_tier(3_000) == "nano"
        assert _classify_tier(1_000) == "nano"
        assert _classify_tier(999) != "nano"

    def test_emerging_tier(self):
        from igautomation.scraper.analyzer import _classify_tier
        assert _classify_tier(500) == "emerging"
        assert _classify_tier(1) == "emerging"
        assert _classify_tier(0) == "emerging"


class TestGrowthStatus:
    """Test compute_growth_status function."""

    def _ts(self, days_ago: int) -> str:
        """Return an ISO timestamp for N days ago."""
        dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
        return dt.isoformat()

    def test_unknown_with_few_snapshots(self):
        from igautomation.scraper.analyzer import compute_growth_status
        status, rate = compute_growth_status([(100, self._ts(1))])
        assert status == "unknown"
        assert rate == 0.0

    def test_rising_growth(self):
        from igautomation.scraper.analyzer import compute_growth_status
        # 100 → 120 over 7 days = +20%/week → rising
        snapshots = [
            (100, self._ts(14)),
            (120, self._ts(7)),
        ]
        status, rate = compute_growth_status(snapshots)
        assert status == "rising"
        assert rate >= 3.0

    def test_stable_growth(self):
        from igautomation.scraper.analyzer import compute_growth_status
        # 1000 → 1010 over 14 days ≈ +0.5%/week → stable
        snapshots = [
            (1000, self._ts(14)),
            (1010, self._ts(0)),
        ]
        status, rate = compute_growth_status(snapshots)
        assert status == "stable"
        assert abs(rate) < 3.0

    def test_declining(self):
        from igautomation.scraper.analyzer import compute_growth_status
        # 1000 → 800 over 7 days = -20%/week → declining
        snapshots = [
            (1000, self._ts(14)),
            (800, self._ts(7)),
        ]
        status, rate = compute_growth_status(snapshots)
        assert status == "declining"
        assert rate <= -3.0

    def test_zero_oldest_count(self):
        from igautomation.scraper.analyzer import compute_growth_status
        snapshots = [
            (0, self._ts(7)),
            (100, self._ts(0)),
        ]
        status, rate = compute_growth_status(snapshots)
        assert status == "unknown"

    def test_multiple_snapshots(self):
        from igautomation.scraper.analyzer import compute_growth_status
        # Growth from 100 → 150 over 14 days = +25%/week
        snapshots = [
            (100, self._ts(28)),
            (120, self._ts(21)),
            (135, self._ts(14)),
            (150, self._ts(0)),
        ]
        status, rate = compute_growth_status(snapshots)
        assert status == "rising"
        assert rate > 10.0


class TestProfileInfoDisplayTag:
    """Test the ProfileInfo display_tag property."""

    def test_tier_only_when_not_rising(self):
        from igautomation.scraper.analyzer import ProfileInfo
        p = ProfileInfo(username="test", tier="micro", growth_status="stable")
        assert p.display_tag == "micro"

    def test_tier_plus_rising(self):
        from igautomation.scraper.analyzer import ProfileInfo
        p = ProfileInfo(username="test", tier="nano", growth_status="rising")
        assert p.display_tag == "nano + rising"

    def test_emerging_plus_rising(self):
        from igautomation.scraper.analyzer import ProfileInfo
        p = ProfileInfo(username="test", tier="emerging", growth_status="rising")
        assert p.display_tag == "emerging + rising"

    def test_tier_labels_exist(self):
        from igautomation.scraper.analyzer import TIER_LABELS
        assert "emerging" in TIER_LABELS
        assert "nano" in TIER_LABELS
        assert len(TIER_LABELS) == 6

    def test_growth_labels_exist(self):
        from igautomation.scraper.analyzer import GROWTH_LABELS
        assert "rising" in GROWTH_LABELS
        assert "stable" in GROWTH_LABELS
        assert "declining" in GROWTH_LABELS
        assert "unknown" in GROWTH_LABELS


class TestExpandedSearchTerms:
    """Test that the expanded discovery lists are non-empty."""

    def test_shoutout_pages_includes_micro(self):
        from igautomation.scraper.collector import BD_SHOUTOUT_PAGES
        # Check some of the new micro/community pages are present
        assert "bd_campus_style" in BD_SHOUTOUT_PAGES
        assert "bangladeshi_upcoming_model" in BD_SHOUTOUT_PAGES
        assert "bd_local_beauty" in BD_SHOUTOUT_PAGES

    def test_hashtags_includes_rising(self):
        from igautomation.scraper.collector import BD_HASHTAGS
        assert "bdupcomingmodel" in BD_HASHTAGS
        assert "bangladeshiemergingmodel" in BD_HASHTAGS
        assert "bdnanoinfluencer" in BD_HASHTAGS

    def test_search_terms_includes_small(self):
        from igautomation.scraper.collector import BD_SEARCH_TERMS
        assert "bangladeshi upcoming model" in BD_SEARCH_TERMS
        assert "bd nano influencer" in BD_SEARCH_TERMS
        assert "bangladeshi student influencer" in BD_SEARCH_TERMS

    def test_original_terms_still_present(self):
        from igautomation.scraper.collector import BD_SEARCH_TERMS
        assert "bangladeshi model" in BD_SEARCH_TERMS
        assert "bd bold model" in BD_SEARCH_TERMS
