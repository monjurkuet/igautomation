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
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
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
]

MODEL_KEYWORDS: list[str] = [
    "model", "influencer", "creator", "fashion", "beauty",
    "actress", "digital creator", "artist", "content creator",
    "blogger", "stylist", "makeup", "glamour", "bold",
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
    tier: str = ""  # mega, macro, mid, micro, nano
    category: str = ""  # fashion, beauty, lifestyle, etc.

    def __post_init__(self) -> None:
        if not self.url:
            self.url = f"https://www.instagram.com/{self.username}/"

    @property
    def follower_str(self) -> str:
        """Human-readable follower count like '101K'."""
        return _format_count(self.follower_count)


def _format_count(n: int) -> str:
    """Format a numeric count into abbreviated string."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _classify_tier(follower_count: int) -> str:
    """Classify an account into an influencer tier by follower count."""
    if follower_count >= 1_000_000:
        return "mega"
    if follower_count >= 100_000:
        return "macro"
    if follower_count >= 25_000:
        return "mid"
    if follower_count >= 5_000:
        return "micro"
    return "nano"


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
            print(f"@{profile.username}: BD={profile.is_bd} Model={profile.is_model} tier={profile.tier}")
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

            # Check session budget if engine is attached
            if self._engine and not self._engine.can_view_profile():
                logger.warning("analyze: session profile-view budget exhausted, stopping")
                break

            info = self._analyze_one(username)
            if info and info.exists:
                results.append(info)
                logger.info(
                    "[%d/%d] @%s: BD=%s Model=%s tier=%s — followers=%s",
                    i + 1,
                    len(usernames),
                    username,
                    info.is_bd,
                    info.is_model,
                    info.tier,
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

        return info

    @staticmethod
    def to_dicts(profiles: list[ProfileInfo]) -> list[dict[str, Any]]:
        """Convert ProfileInfo list to list of dicts for JSON/CSV export."""
        return [asdict(p) for p in profiles]

    @staticmethod
    def filter_bd_models(profiles: list[ProfileInfo]) -> list[ProfileInfo]:
        """Return only profiles that match BD and/or model keywords."""
        return [p for p in profiles if p.is_bd or p.is_model]
