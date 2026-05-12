"""Profile analyzer — verify accounts and enrich metadata via GraphQL.

Refactored to use ONLY GraphQL API calls — no page navigation or DOM
scraping. This is faster, more reliable, and generates far fewer
suspicious signals on Instagram's end.

What changed from v1:
- Removed navigate() + document.body.innerText scraping
- Uses get_web_profile_info() for full profile data (bio, counts, name)
- Parses numeric counts directly from API (no "101K" string parsing)
- Keyword matching now runs on bio + full_name (API fields), not DOM text
- Optional BehaviorEngine integration for organic timing between checks
- Expanded tier system: mega/macro/mid/micro/nano/emerging
- Growth status overlay: rising/stable/declining/unknown
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from igautomation.cdp.client import CDPClient
from igautomation.graphql.client import GraphQLClient

if TYPE_CHECKING:
    from igautomation.behavior.engine import BehaviorEngine

logger = logging.getLogger(__name__)

# Keywords that suggest a Bangladeshi model/influencer profile.
BD_KEYWORDS: list[str] = [
    "bangladesh", "bangladeshi", "bd", "deshi", "dhaka", "chittagong",
    "ctg", "sylhet", "rajshahi", "khulna", "comilla", "gazipur",
    "narayanganj", "বাংলা", "ঢাকা", "🇧🇩", "bengali",
    "cox's bazar", "rangpur", "mymensingh", "barishal", "bogra",
    "tongi", "savlon", "narsingdi", "brahmanbaria",
]

MODEL_KEYWORDS: list[str] = [
    "model", "influencer", "creator", "fashion", "beauty",
    "actress", "digital creator", "artist", "content creator",
    "blogger", "stylist", "makeup", "glamour", "bold",
    "lifestyle", "tiktok", "reel creator", "vlogger",
    "entrepreneur", "public figure", "student influencer",
]

# Niche keywords for small/growing accounts that signal potential
RISING_SIGNAL_KEYWORDS: list[str] = [
    "just started", "new account", "beginner", "growing",
    "college", "university", "student", "campus",
    "upcoming", "aspiring", "fresh face", "new face",
    "local", "small town", "district", "village",
]


@dataclass
class ProfileInfo:
    """Parsed profile information for a single Instagram account."""

    username: str
    url: str = ""
    exists: bool = True
    full_name: str = ""
    meta_description: str = ""
    follower_count: int = 0
    following_count: int = 0
    post_count: int = 0
    bio: str = ""
    is_private: bool = False
    is_verified: bool = False
    profile_pic_url: str = ""
    is_bd: bool = False
    is_model: bool = False
    bd_keywords_matched: list[str] = field(default_factory=list)
    model_keywords_matched: list[str] = field(default_factory=list)

    # Classification fields (populated by analyzer or LLM)
    tier: str = ""  # mega, macro, mid, micro, nano, emerging
    category: str = ""  # fashion, beauty, lifestyle, etc.
    growth_status: str = "unknown"  # rising, stable, declining, unknown

    def __post_init__(self) -> None:
        if not self.url:
            self.url = f"https://www.instagram.com/{self.username}/"

    @property
    def follower_str(self) -> str:
        """Human-readable follower count like '101K'."""
        return _format_count(self.follower_count)

    @property
    def tier_label(self) -> str:
        """Emoji tier label like '🔥 Mid (25K–100K)'."""
        return TIER_LABELS.get(self.tier, self.tier)

    @property
    def growth_label(self) -> str:
        """Emoji growth label like '📈 Rising'."""
        return GROWTH_LABELS.get(self.growth_status, self.growth_status)

    @property
    def display_tag(self) -> str:
        """Combined tag like 'micro + rising' or just 'mid'."""
        if self.growth_status == "rising":
            return f"{self.tier} + rising"
        return self.tier


def _format_count(n: int) -> str:
    """Format a numeric count into abbreviated string."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _classify_tier(follower_count: int) -> str:
    """Classify an account into an influencer tier by follower count.

    Tiers:
        mega     — 1M+ followers (celebrities, national figures)
        macro    — 100K–1M (established influencers, brand ambassadors)
        mid      — 25K–100K (solid influencers, regional reach)
        micro    — 5K–25K (niche influencers, engaged communities)
        nano     — 1K–5K (small but real following)
        emerging — <1K (just starting out, has potential signals)
    """
    if follower_count >= 1_000_000:
        return "mega"
    if follower_count >= 100_000:
        return "macro"
    if follower_count >= 25_000:
        return "mid"
    if follower_count >= 5_000:
        return "micro"
    if follower_count >= 1_000:
        return "nano"
    return "emerging"


def compute_growth_status(
    snapshots: list[tuple[int, str]],
    min_snapshots: int = 2,
) -> tuple[str, float]:
    """Compute growth status and rate from follower snapshots.

    Args:
        snapshots: List of (follower_count, iso_timestamp) tuples, oldest first.
        min_snapshots: Minimum snapshots needed to determine growth.

    Returns:
        (growth_status, growth_rate) where:
        - growth_status: "rising", "stable", "declining", "unknown"
        - growth_rate: weekly percentage change (e.g. 5.2 means +5.2%/week)
    """
    if len(snapshots) < min_snapshots:
        return ("unknown", 0.0)

    oldest_count, oldest_ts = snapshots[0]
    newest_count, newest_ts = snapshots[-1]

    if oldest_count <= 0:
        return ("unknown", 0.0)

    try:
        t_old = datetime.fromisoformat(oldest_ts.replace("Z", "+00:00"))
        t_new = datetime.fromisoformat(newest_ts.replace("Z", "+00:00"))
        days = (t_new - t_old).days
        if days <= 0:
            return ("unknown", 0.0)
    except (ValueError, TypeError):
        return ("unknown", 0.0)

    raw_rate = (newest_count - oldest_count) / oldest_count * 100
    weekly_rate = raw_rate / days * 7

    if weekly_rate >= 3.0:
        return ("rising", round(weekly_rate, 2))
    if weekly_rate <= -3.0:
        return ("declining", round(weekly_rate, 2))
    return ("stable", round(weekly_rate, 2))


# "Rising" is an overlay — an account can be any static tier AND rising.
# Example: a "micro" account with +10%/week growth is "micro + rising".
TIER_LABELS: dict[str, str] = {
    "mega": "🏆 Mega (1M+)",
    "macro": "⭐ Macro (100K–1M)",
    "mid": "🔥 Mid (25K–100K)",
    "micro": "📌 Micro (5K–25K)",
    "nano": "🌱 Nano (1K–5K)",
    "emerging": "✨ Emerging (<1K)",
}

GROWTH_LABELS: dict[str, str] = {
    "rising": "📈 Rising",
    "stable": "➡️ Stable",
    "declining": "📉 Declining",
    "unknown": "❓ Unknown",
}


class ProfileAnalyzer:
    """Verify and enrich profile metadata using GraphQL only.

    Optionally accepts a BehaviorEngine for organic timing between
    profile checks. Without an engine, runs as fast as possible
    (for backward compatibility).

    Usage::

        cdp = CDPClient()
        cdp.connect(ws_url)

        analyzer = ProfileAnalyzer(cdp)
        results = analyzer.analyze(["z.subha_", "anonna_fatima"])
        for profile in results:
            print(f"@{profile.username}: {profile.display_tag} — followers={profile.follower_str}")
    """

    def __init__(
        self,
        cdp: CDPClient,
        graphql: GraphQLClient | None = None,
        engine: BehaviorEngine | None = None,
    ) -> None:
        self._cdp = cdp
        self._graphql = graphql or GraphQLClient(cdp)
        self._engine = engine

    def analyze(
        self,
        usernames: list[str],
        skip_existing: bool = True,
        known_good: set[str] | None = None,
    ) -> list[ProfileInfo]:
        """Analyze a list of usernames and return profile info.

        Args:
            usernames: List of usernames to analyze.
            skip_existing: Skip usernames in known_good set.
            known_good: Set of usernames already verified.

        Returns:
            List of ProfileInfo for profiles that exist.
        """
        if known_good is None:
            known_good = set()

        results: list[ProfileInfo] = []
        for i, username in enumerate(usernames):
            if skip_existing and username in known_good:
                continue

            # Stop if GraphQL client is rate-limited
            if self._graphql.rate_limited:
                logger.warning(
                    "analyze: rate-limited by Instagram (429), stopping at %d/%d",
                    i, len(usernames),
                )
                break

            # Check session budget if engine is attached
            if self._engine and not self._engine.can_view_profile():
                logger.warning("analyze: session profile-view budget exhausted, stopping")
                break

            info = self._analyze_one(username)
            if info and info.exists:
                results.append(info)
                logger.info(
                    "[%d/%d] @%s: %s — followers=%s",
                    i + 1,
                    len(usernames),
                    username,
                    info.display_tag,
                    info.follower_str,
                )
            else:
                logger.debug("[%d/%d] @%s: not found or error", i + 1, len(usernames), username)

            # Organic delay between profile checks
            if self._engine:
                self._engine._delay()
                self._engine._session.profile_views_used += 1

        return results

    def _analyze_one(self, username: str) -> ProfileInfo | None:
        """Analyze a single profile using GraphQL only — no page navigation."""
        info = ProfileInfo(username=username)

        # Use web_profile_info API — returns full profile data
        profile_data = self._graphql.get_web_profile_info(username)
        if not profile_data:
            info.exists = False
            return info

        user = profile_data
        info.exists = True
        info.full_name = user.get("full_name", "")
        info.bio = user.get("biography", "")
        info.is_private = user.get("is_private", False)
        info.is_verified = user.get("is_verified", False)
        info.profile_pic_url = user.get("profile_pic_url", "")

        # Numeric counts directly from API — no string parsing needed
        edge_followed_by = user.get("edge_followed_by", {})
        info.follower_count = edge_followed_by.get("count", 0) if isinstance(edge_followed_by, dict) else 0
        edge_follow = user.get("edge_follow", {})
        info.following_count = edge_follow.get("count", 0) if isinstance(edge_follow, dict) else 0
        edge_owner_to_timeline = user.get("edge_owner_to_timeline_media", {})
        info.post_count = edge_owner_to_timeline.get("count", 0) if isinstance(edge_owner_to_timeline, dict) else 0

        # Build meta_description for compatibility
        info.meta_description = (
            f"{info.follower_str} Followers, {info.following_count} Following, "
            f"{info.post_count} Posts - See Instagram photos and videos"
        )

        # Classify tier
        info.tier = _classify_tier(info.follower_count)

        # Keyword matching on bio + full_name (no DOM scraping)
        combined = f"{info.bio} {info.full_name}".lower()

        for kw in BD_KEYWORDS:
            if kw.lower() in combined:
                info.is_bd = True
                info.bd_keywords_matched.append(kw)

        for kw in MODEL_KEYWORDS:
            if kw.lower() in combined:
                info.is_model = True
                info.model_keywords_matched.append(kw)

        # Detect rising signals for small accounts
        if info.tier in ("emerging", "nano", "micro"):
            for kw in RISING_SIGNAL_KEYWORDS:
                if kw.lower() in combined:
                    info.growth_status = "rising"
                    break

        # Set category from keyword matches
        if info.is_bd and info.is_model:
            info.category = "bd_model"
        elif info.is_bd:
            info.category = "bd"
        elif info.is_model:
            info.category = "model"
        else:
            info.category = ""

        return info

    @staticmethod
    def to_dicts(profiles: list[ProfileInfo]) -> list[dict[str, Any]]:
        """Convert ProfileInfo list to list of dicts for JSON/CSV export."""
        return [asdict(p) for p in profiles]

    @staticmethod
    def filter_bd_models(profiles: list[ProfileInfo]) -> list[ProfileInfo]:
        """Return only profiles that match BD and/or model keywords."""
        return [p for p in profiles if p.is_bd or p.is_model]

    @staticmethod
    def filter_rising(profiles: list[ProfileInfo]) -> list[ProfileInfo]:
        """Return only profiles flagged as rising (small + growing)."""
        return [p for p in profiles if p.growth_status == "rising"]
