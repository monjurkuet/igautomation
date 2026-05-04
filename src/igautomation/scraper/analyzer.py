"""Profile analyzer — verify accounts and enrich metadata.

Given a list of usernames, navigates to each profile and extracts:
- Follower/following/post counts (from og:description)
- Full name and bio
- Whether the profile exists
- BD/model keywords matching
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from igautomation.cdp.client import CDPClient
from igautomation.graphql.client import GraphQLClient

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
    follower_count: str = ""
    following_count: str = ""
    post_count: str = ""
    bio: str = ""
    is_bd: bool = False
    is_model: bool = False
    bd_keywords_matched: list[str] = field(default_factory=list)
    model_keywords_matched: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.url:
            self.url = f"https://www.instagram.com/{self.username}/"


class ProfileAnalyzer:
    """Verify and enrich profile metadata for a list of usernames.

    Usage::

        cdp = CDPClient()
        cdp.connect(ws_url)

        analyzer = ProfileAnalyzer(cdp)
        results = analyzer.analyze(["z.subha_", "anonna_fatima"])
        for profile in results:
            print(f"@{profile.username}: BD={profile.is_bd} Model={profile.is_model}")
    """

    def __init__(self, cdp: CDPClient, graphql: GraphQLClient | None = None) -> None:
        self._cdp = cdp
        self._graphql = graphql or GraphQLClient(cdp)

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

            info = self._analyze_one(username)
            if info and info.exists:
                results.append(info)
                logger.info(
                    "[%d/%d] @%s: BD=%s Model=%s — %s",
                    i + 1,
                    len(usernames),
                    username,
                    info.is_bd,
                    info.is_model,
                    info.meta_description[:60],
                )
            else:
                logger.debug("[%d/%d] @%s: not found or error", i + 1, len(usernames), username)

            time.sleep(0.3)

        return results

    def _analyze_one(self, username: str) -> ProfileInfo | None:
        """Analyze a single profile."""
        info = ProfileInfo(username=username)
        meta = self._graphql.get_profile_meta(username)
        if not meta:
            info.exists = False
            return info

        info.meta_description = meta.get("meta", "")

        # Parse follower/post/following counts from meta description
        # Format: "101K Followers, 342 Following, 852 Posts - See Instagram..."
        self._parse_meta_counts(info)

        # Get body text for deeper analysis
        body_raw = self._cdp.evaluate(
            "document.body.innerText.substring(0, 1500)", timeout=10
        )
        body_text = body_raw or ""
        combined = f"{info.meta_description} {body_text}".lower()

        # Check BD keywords
        for kw in BD_KEYWORDS:
            if kw.lower() in combined:
                info.is_bd = True
                info.bd_keywords_matched.append(kw)

        # Check model keywords
        for kw in MODEL_KEYWORDS:
            if kw.lower() in combined:
                info.is_model = True
                info.model_keywords_matched.append(kw)

        # Extract full name from meta description
        # "Zarin Subha Khan (@z.subha_) • Instagram photos and videos"
        title_match = re.match(r"(.+?)\s*\(@" + re.escape(username) + r"\)", meta.get("title", ""))
        if title_match:
            info.full_name = title_match.group(1).strip()

        return info

    @staticmethod
    def _parse_meta_counts(info: ProfileInfo) -> None:
        """Parse follower/following/post counts from og:description."""
        meta = info.meta_description
        if not meta:
            return

        # Try pattern: "101K Followers, 342 Following, 852 Posts"
        follower_match = re.search(r"([\d,.KkMm]+)\s+Followers?", meta, re.IGNORECASE)
        if follower_match:
            info.follower_count = follower_match.group(1)

        following_match = re.search(r"([\d,.KkMm]+)\s+Following", meta, re.IGNORECASE)
        if following_match:
            info.following_count = following_match.group(1)

        post_match = re.search(r"([\d,.KkMm]+)\s+Posts?", meta, re.IGNORECASE)
        if post_match:
            info.post_count = post_match.group(1)

    @staticmethod
    def to_dicts(profiles: list[ProfileInfo]) -> list[dict[str, Any]]:
        """Convert ProfileInfo list to list of dicts for JSON/CSV export."""
        return [asdict(p) for p in profiles]

    @staticmethod
    def filter_bd_models(profiles: list[ProfileInfo]) -> list[ProfileInfo]:
        """Return only profiles that match BD and/or model keywords."""
        return [p for p in profiles if p.is_bd or p.is_model]
