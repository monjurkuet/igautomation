"""Account discovery and collection strategies.

Provides multiple discovery strategies that can be combined:
- **Shoutout page scraping**: Visit BD model shoutout/feature pages,
  scroll to load content, collect profile links.
- **GraphQL suggestions**: For each known user, fetch Instagram's
  "Suggested for you" accounts via GraphQL.
- **Hashtag exploration**: Visit hashtag pages and collect accounts
  from top/recent posts.
- **Cascading discovery**: For every account found, fetch *their*
  suggestions — this expands the graph exponentially.
- **Search API**: Use Instagram's user search endpoint.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from igautomation.cdp.client import CDPClient, SKIP_USERNAMES
from igautomation.cdp.discovery import TabDiscovery
from igautomation.graphql.client import GraphQLClient

logger = logging.getLogger(__name__)

# Common BD model shoutout/feature page usernames.
BD_SHOUTOUT_PAGES: list[str] = [
    "deshi_girl_shoutout",
    "bangladeshi_hot_model_",
    "bangladeshi_hot_girls_",
    "bangladeshi_beauties_",
    "bd_fashion_model",
    "bd_beauties_",
    "deshi_girls_fashion_",
    "bd_girls_fashion",
    "beautiful_bangladeshi",
    "deshi_models_bd",
    "bd_girls_style",
    "deshi_hot_girls_",
    "bangladeshi_girls_",
    "bd_girls_shoutout",
    "deshi_girls_reels",
    "bd_influencer_hub",
    "bd_girls_world",
    "bd_girls_hub",
    "deshi_beauties_",
    "bangladeshi_model_hub",
    "bangladeshi_girls_shoutout",
    "deshi_model_hub",
    "bd_model_gallery",
    "deshi_hot_models",
    "bangladeshi_girls_fashion",
    "bd_glamour_model",
    "bangladeshi_model_star",
    "deshi_hot_girls",
    "bd_hot_girls_shoutout",
    "bangladeshi_girls_beauty",
    "bangladeshi_model_queens",
    "bd_hot_queens_",
    "deshi_queens_hub",
    "bd_beauties_hub",
    "bangladeshi_hot_beauties",
    "deshi_hot_queens",
    "bd_girls_gram",
    "bangladeshi_girls_gram",
    "deshi_girls_hub_",
    "bangladeshi_hot_queens",
    "bd_model_shoutout",
    "bd_model_queen",
    "bangladeshi_model_world",
    "deshi_model_queen",
    "bd_beauty_queens",
    "bd_girls_official",
    "bangladeshi_girls_official",
    "deshi_girls_official",
    "bd_hot_girls_official",
    "bangladeshi_tiktok_girls",
    "bd_tiktok_girls",
    "deshi_tiktok_girls",
    "bangladeshi_reel_queens",
    "bd_hot_reels",
    "deshi_reel_girls",
    "bd_reel_model",
    "deshi_girls_reels",
    "bangladeshi_girls_reels",
    "bd_reel_queens",
    "deshi_reel_queens",
    "bangladeshi_reel_model",
]

BD_HASHTAGS: list[str] = [
    "bangladeshimodel",
    "bangladeshibeauty",
    "bdmodel",
    "deshimodel",
    "bangladeshigirl",
    "dhakamodel",
    "bangladeshifashion",
    "bangladeshiglamour",
    "bdbold",
    "bangladeshibold",
    "deshibeauty",
    "bangladeshiinfluencer",
    "bdgirl",
    "bangladeshimodels",
    "dhakagirls",
    "bangladeshifashionmodel",
    "bdstyle",
    "deshigirls",
    "bangladeshidigitalcreator",
    "bangladeshifashionblogger",
    "bdfashion",
]

BD_SEARCH_TERMS: list[str] = [
    "bangladeshi model",
    "bangladeshi girl",
    "bd model",
    "deshi model",
    "dhaka model",
    "bangladeshi fashion",
    "bangladeshi beauty",
    "bangladeshi influencer",
    "bd bold model",
    "bangladeshi bold",
]


def _is_individual_account(username: str) -> bool:
    """Return True if the username looks like an individual, not a page/hub."""
    lower = username.lower()
    non_individual_keywords = [
        "shoutout", "hub", "beauties", "girls_", "fashion",
        "model_world", "_model_", "hot_girls", "gram",
        "spotlight", "queens", "baddies", "reel", "tiktok",
        "glamour", "creator", "influencer", "beauty", "style",
        "world", "official", "_bd", "bangladeshi_",
        "gallery", "star", "official_",
    ]
    return not any(kw in lower for kw in non_individual_keywords)


class AccountCollector:
    """High-level account discovery combining multiple strategies.

    Usage::

        cdp = CDPClient()
        cdp.connect(ws_url)

        collector = AccountCollector(cdp)
        accounts = collector.collect(seed_usernames=["z.subha_"], target_count=100)
    """

    def __init__(self, cdp: CDPClient, graphql: GraphQLClient | None = None) -> None:
        self._cdp = cdp
        self._graphql = graphql or GraphQLClient(cdp)
        self._accounts: set[str] = set()
        self._user_ids: dict[str, str] = {}
        self._callbacks: list[Callable[[str, int], None]] = []

    @property
    def accounts(self) -> set[str]:
        """Current set of discovered usernames."""
        return self._accounts

    @property
    def user_ids(self) -> dict[str, str]:
        """Mapping of username -> numeric user ID."""
        return self._user_ids

    def on_progress(self, callback: Callable[[str, int], None]) -> None:
        """Register a progress callback. Called with (message, total_count)."""
        self._callbacks.append(callback)

    def _emit(self, msg: str) -> None:
        total = len(self._accounts)
        logger.info(msg)
        for cb in self._callbacks:
            try:
                cb(msg, total)
            except Exception:
                pass

    def _add(self, username: str) -> None:
        if username.lower() in SKIP_USERNAMES:
            return
        if len(username) < 2:
            return
        self._accounts.add(username)

    def _add_many(self, usernames: list[str]) -> int:
        before = len(self._accounts)
        for u in usernames:
            self._add(u)
        return len(self._accounts) - before

    # ------------------------------------------------------------------
    # Strategy 1: Existing tabs
    # ------------------------------------------------------------------
    def scrape_existing_tabs(self, base_url: str = "http://localhost:9224") -> int:
        """Collect profile links from already-open Instagram tabs.

        Returns:
            Number of new accounts added.
        """
        self._emit("Scanning existing Chrome tabs...")
        tabs = TabDiscovery.get_ig_tabs(base_url)
        js = """
        (function() {
            var p = [];
            document.querySelectorAll('a[href]').forEach(function(a) {
                var h = a.getAttribute('href');
                if (h && /^\\/[a-zA-Z0-9._]{2,30}\\/?$/.test(h)) {
                    var name = h.replace(/\\/$/,'').replace(/^\\//,'');
                    p.push(name);
                }
            });
            return JSON.stringify([...new Set(p)]);
        })()
        """
        for tab in tabs:
            ws_url = tab.get("webSocketDebuggerUrl", "")
            if not ws_url:
                continue
            self._cdp.connect(ws_url)
            raw = self._cdp.evaluate(js, timeout=10)
            if raw:
                try:
                    profiles = json_imports(raw)
                    new = self._add_many(profiles)
                    self._emit(f"  Tab {tab.get('url','')}: +{new} ({len(self._accounts)} total)")
                except Exception:
                    pass
        return len(self._accounts)

    # ------------------------------------------------------------------
    # Strategy 2: Shoutout pages
    # ------------------------------------------------------------------
    def scrape_shoutout_pages(
        self,
        pages: list[str] | None = None,
        max_per_page: int = 12,
    ) -> int:
        """Visit shoutout/feature pages, scroll, and collect profile links.

        Args:
            pages: List of shoutout page usernames. Uses BD_SHOUTOUT_PAGES
                if not provided.
            max_per_page: Max scroll iterations per page.

        Returns:
            Number of new accounts added.
        """
        pages = pages or BD_SHOUTOUT_PAGES
        self._emit(f"Scraping {len(pages)} shoutout pages...")
        before = len(self._accounts)

        for i, page in enumerate(pages):
            if len(self._accounts) >= 500:
                self._emit("Hit account limit, stopping shoutout scraping")
                break

            url = f"https://www.instagram.com/{page}/"
            self._cdp.navigate(url, wait=3)

            # Quick 404 check
            title = self._cdp.evaluate("document.title", timeout=5) or ""
            if "not found" in title.lower():
                continue

            # Scroll and collect
            found = self._cdp.scroll(max_scrolls=max_per_page, delay=1.5)
            new = self._add_many(found)
            if new > 0:
                self._emit(f"  [{i+1}] @{page}: +{new} ({len(self._accounts)} total)")

            time.sleep(0.3)

        return len(self._accounts) - before

    # ------------------------------------------------------------------
    # Strategy 3: GraphQL suggestions
    # ------------------------------------------------------------------
    def fetch_suggestions(self, usernames: list[str]) -> int:
        """Fetch suggested accounts via GraphQL for each username.

        Resolves each username to a user ID, then queries the suggestion
        API. New usernames are added to the collection.

        Args:
            usernames: List of usernames to get suggestions for.

        Returns:
            Number of new accounts added.
        """
        self._emit(f"Fetching GraphQL suggestions for {len(usernames)} profiles...")
        before = len(self._accounts)

        for username in usernames:
            # Resolve user ID (navigate to profile)
            uid = self._user_ids.get(username)
            if not uid:
                uid = self._graphql.get_user_id(username)
                if uid:
                    self._user_ids[username] = uid
                else:
                    self._emit(f"  @{username}: could not resolve user ID")
                    continue

            # Fetch suggestions
            suggested = self._graphql.get_suggested_users(uid)
            new = self._add_many(suggested)
            if new > 0:
                self._emit(f"  @{username}: +{new} suggestions ({len(self._accounts)} total)")

            time.sleep(0.3)

        return len(self._accounts) - before

    # ------------------------------------------------------------------
    # Strategy 4: Hashtag pages
    # ------------------------------------------------------------------
    def scrape_hashtags(
        self,
        hashtags: list[str] | None = None,
        max_scrolls: int = 8,
    ) -> int:
        """Visit hashtag explore pages and collect profile links.

        Args:
            hashtags: List of hashtags (without #). Uses BD_HASHTAGS
                if not provided.
            max_scrolls: Max scroll iterations per hashtag page.

        Returns:
            Number of new accounts added.
        """
        hashtags = hashtags or BD_HASHTAGS
        self._emit(f"Scraping {len(hashtags)} hashtag pages...")
        before = len(self._accounts)

        for tag in hashtags:
            url = f"https://www.instagram.com/explore/tags/{tag}/"
            self._cdp.navigate(url, wait=3)

            found = self._cdp.scroll(max_scrolls=max_scrolls, delay=1.5)
            new = self._add_many(found)
            if new > 0:
                self._emit(f"  #{tag}: +{new} ({len(self._accounts)} total)")

            time.sleep(0.3)

        return len(self._accounts) - before

    # ------------------------------------------------------------------
    # Strategy 5: User search
    # ------------------------------------------------------------------
    def search_users(self, queries: list[str] | None = None) -> int:
        """Search for users via Instagram's search API.

        Args:
            queries: Search terms. Uses BD_SEARCH_TERMS if not provided.

        Returns:
            Number of new accounts added.
        """
        queries = queries or BD_SEARCH_TERMS
        self._emit(f"Searching {len(queries)} terms...")
        before = len(self._accounts)

        # Make sure we're on an Instagram page for the fetch() to work
        self._cdp.navigate("https://www.instagram.com/explore/", wait=2)

        for query in queries:
            users = self._graphql.search_users(query)
            usernames = [u["username"] for u in users if u.get("username")]
            new = self._add_many(usernames)
            # Also cache user IDs
            for u in users:
                if u.get("username") and u.get("pk"):
                    self._user_ids[u["username"]] = u["pk"]
            if new > 0:
                self._emit(f"  '{query}': +{new} ({len(self._accounts)} total)")

            time.sleep(0.3)

        return len(self._accounts) - before

    # ------------------------------------------------------------------
    # Strategy 6: Cascading discovery
    # ------------------------------------------------------------------
    def cascade_suggestions(self, max_depth: int = 2, max_profiles: int = 50, target_count: int = 0) -> int:
        """For each individual account found, fetch THEIR suggestions.

        This is the most powerful strategy — it expands the account graph
        exponentially. Only individual accounts (not shoutout/hub pages)
        are used as seeds for further suggestion fetches.

        Args:
            max_depth: How many cascade rounds to run.
            max_profiles: Max profiles to process per round.
            target_count: If > 0, stop cascade once this many total
                accounts are collected.

        Returns:
            Total new accounts added across all rounds.
        """
        total_new = 0
        processed: set[str] = set()

        for depth in range(max_depth):
            if target_count > 0 and len(self._accounts) >= target_count:
                self._emit(f"Cascade: target of {target_count} reached — stopping")
                break

            # Find individual accounts we haven't processed yet
            candidates = sorted(
                u for u in self._accounts
                if u not in processed and _is_individual_account(u)
            )
            candidates = candidates[:max_profiles]

            if not candidates:
                self._emit(f"Cascade round {depth+1}: no new candidates")
                break

            self._emit(
                f"Cascade round {depth+1}/{max_depth}: "
                f"fetching suggestions for {len(candidates)} profiles"
            )

            before = len(self._accounts)
            for username in candidates:
                if target_count > 0 and len(self._accounts) >= target_count:
                    break

                processed.add(username)

                uid = self._user_ids.get(username)
                if not uid:
                    uid = self._graphql.get_user_id(username)
                    if uid:
                        self._user_ids[username] = uid
                    else:
                        continue

                suggested = self._graphql.get_suggested_users(uid)
                new = self._add_many(suggested)
                if new > 0:
                    self._emit(f"  @{username}: +{new} ({len(self._accounts)} total)")

                time.sleep(0.3)

            round_new = len(self._accounts) - before
            total_new += round_new
            self._emit(f"Cascade round {depth+1} done: +{round_new} accounts")

            if round_new == 0:
                break

        return total_new

    # ------------------------------------------------------------------
    # Master collect method
    # ------------------------------------------------------------------
    def collect(
        self,
        seed_usernames: list[str] | None = None,
        target_count: int = 100,
        strategies: list[str] | None = None,
    ) -> list[str]:
        """Run all discovery strategies to collect accounts.

        Args:
            seed_usernames: Starting usernames to bootstrap discovery.
            target_count: Stop when this many accounts are collected.
            strategies: Ordered list of strategy names to run.
                Default: all strategies in order.

        Returns:
            Sorted list of discovered usernames.
        """
        all_strategies = [
            "existing_tabs",
            "shoutout_pages",
            "graphql_suggestions",
            "search",
            "hashtags",
            "cascade",
        ]
        strategies = strategies or all_strategies

        # Add seeds
        if seed_usernames:
            for u in seed_usernames:
                self._add(u)

        for strategy in strategies:
            if len(self._accounts) >= target_count:
                self._emit(f"Target of {target_count} reached — stopping")
                break

            match strategy:
                case "existing_tabs":
                    self.scrape_existing_tabs()
                case "shoutout_pages":
                    self.scrape_shoutout_pages()
                case "graphql_suggestions":
                    seeds = list(self._accounts)
                    self.fetch_suggestions(seeds[:30])
                case "search":
                    self.search_users()
                case "hashtags":
                    self.scrape_hashtags()
                case "cascade":
                    self.cascade_suggestions(max_depth=2, max_profiles=30, target_count=target_count)
                case _:
                    logger.warning("Unknown strategy: %s", strategy)

        return self.get_sorted()

    def get_sorted(self) -> list[str]:
        """Return collected usernames as a sorted list."""
        return sorted(self._accounts)


def json_imports(raw: str) -> list[str]:
    """Parse a JSON list of strings, returning empty list on failure."""
    import json as _json
    try:
        return _json.loads(raw)
    except Exception:
        return []
