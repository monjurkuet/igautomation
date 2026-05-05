"""BehaviorEngine — wraps CDPClient with human-like timing and session budgets.

Every action method:
1. Checks session and daily budgets before proceeding.
2. Adds a randomised delay (``_delay``) between actions.
3. Adds a read/dwell delay (``_dwell``) to simulate human reading.
4. Increments the relevant session and daily counters on success.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable

from igautomation.behavior.config import BehaviorConfig, SessionConfig
from igautomation.cdp.client import CDPClient
from igautomation.graphql.client import GraphQLClient

logger = logging.getLogger(__name__)


class BehaviorEngine:
    """Wraps CDPClient to add human-like timing, budgets, and session management.

    Parameters
    ----------
    cdp : CDPClient
        Low-level CDP client for browser interaction.
    config : BehaviorConfig
        Timing and budget configuration.
    session : SessionConfig
        Mutable session state tracking usage.
    """

    def __init__(
        self,
        cdp: CDPClient,
        config: BehaviorConfig,
        session: SessionConfig,
    ) -> None:
        self._cdp = cdp
        self._config = config
        self._session = session

        # Daily counters — persist across sessions within a day.
        self._daily_likes: int = 0
        self._daily_follows: int = 0
        self._daily_profile_views: int = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _delay(self) -> None:
        """Sleep for a random inter-action delay."""
        secs = self._config.action_delay()
        logger.debug("action delay: %.2fs", secs)
        time.sleep(secs)

    def _dwell(self) -> None:
        """Sleep for a random read/dwell delay (simulates human reading)."""
        secs = self._config.read_dwell()
        logger.debug("dwell: %.2fs", secs)
        time.sleep(secs)

    # ------------------------------------------------------------------
    # Budget check methods (session + daily)
    # ------------------------------------------------------------------

    def can_like(self) -> bool:
        """Return True if both session and daily like budgets are available."""
        return self._session.can_like() and self._daily_likes < self._config.daily_likes_max

    def can_follow(self) -> bool:
        """Return True if both session and daily follow budgets are available."""
        return self._session.can_follow() and self._daily_follows < self._config.daily_follows_max

    def can_view_profile(self) -> bool:
        """Return True if both session and daily profile-view budgets are available."""
        return (
            self._session.can_view_profile()
            and self._daily_profile_views < self._config.daily_profile_views_max
        )

    # ------------------------------------------------------------------
    # High-level actions
    # ------------------------------------------------------------------

    def scroll_feed(self, max_scrolls: int = 5) -> list[str]:
        """Scroll the feed, collecting usernames with human-like timing.

        Parameters
        ----------
        max_scrolls : int
            Maximum number of scroll operations.

        Returns
        -------
        list[str]
            Deduplicated list of usernames found.
        """
        self._delay()
        delay = self._config.scroll_delay()
        logger.info("scroll_feed: max_scrolls=%d delay=%.2fs", max_scrolls, delay)
        usernames = self._cdp.scroll(max_scrolls=max_scrolls, delay=delay)
        return usernames

    def view_profile(self, username: str) -> dict | None:
        """Navigate to a profile, dwell to read, then return metadata.

        Parameters
        ----------
        username : str
            Instagram username (without @).

        Returns
        -------
        dict | None
            Profile metadata dict, or None on failure / budget exhausted.
        """
        if not self.can_view_profile():
            logger.warning("view_profile: budget exhausted for @%s", username)
            return None

        self._delay()
        url = f"https://www.instagram.com/{username}/"
        self._cdp.navigate(url, wait=3)
        self._dwell()

        # Increment counters
        self._session.profile_views_used += 1
        self._daily_profile_views += 1
        logger.info(
            "view_profile: @%s (session=%d, daily=%d)",
            username,
            self._session.profile_views_used,
            self._daily_profile_views,
        )

        # Try to extract profile metadata via JS
        js = """
        (function() {
            var meta = document.querySelector('meta[property="og:description"]');
            return JSON.stringify({
                meta: meta ? meta.getAttribute('content') : '',
                title: document.title
            });
        })()
        """
        raw = self._cdp.evaluate(js, timeout=10)
        if raw:
            import json

            try:
                data = json.loads(raw)
                if "not found" in (data.get("title", "") + data.get("meta", "")).lower():
                    return None
                return data
            except json.JSONDecodeError:
                pass
        return {"username": username}

    def like_post(self, post_url: str) -> bool:
        """Navigate to a post, dwell, click the like button.

        Parameters
        ----------
        post_url : str
            Full Instagram post URL.

        Returns
        -------
        bool
            True if the like action was performed, False otherwise.
        """
        if not self.can_like():
            logger.warning("like_post: budget exhausted for %s", post_url)
            return False

        self._delay()
        self._cdp.navigate(post_url, wait=3)
        self._dwell()

        # Click the like button
        js = """
        (function() {
            var svg = document.querySelector('svg[aria-label="Like"]');
            if (svg) {
                var btn = svg.closest('button') || svg.parentElement;
                if (btn) { btn.click(); return 'liked'; }
            }
            // Try the "Like" text button
            var spans = document.querySelectorAll('span, button');
            for (var i = 0; i < spans.length; i++) {
                if (spans[i].textContent.trim() === 'Like') {
                    spans[i].click(); return 'liked';
                }
            }
            return 'not_found';
        })()
        """
        result = self._cdp.evaluate(js, timeout=10)

        if result == "liked":
            self._session.likes_used += 1
            self._daily_likes += 1
            logger.info(
                "like_post: %s (session=%d, daily=%d)",
                post_url,
                self._session.likes_used,
                self._daily_likes,
            )
            return True

        logger.warning("like_post: like button not found for %s", post_url)
        return False

    def follow_user(self, username: str) -> bool:
        """Navigate to a profile, dwell, click the follow button.

        Parameters
        ----------
        username : str
            Instagram username (without @).

        Returns
        -------
        bool
            True if the follow action was performed, False otherwise.
        """
        if not self.can_follow():
            logger.warning("follow_user: budget exhausted for @%s", username)
            return False

        self._delay()
        url = f"https://www.instagram.com/{username}/"
        self._cdp.navigate(url, wait=3)
        self._dwell()

        # Click the Follow button
        js = """
        (function() {
            var btns = document.querySelectorAll('button');
            for (var i = 0; i < btns.length; i++) {
                var t = btns[i].textContent.trim();
                if (t === 'Follow') { btns[i].click(); return 'followed'; }
            }
            return 'not_found';
        })()
        """
        result = self._cdp.evaluate(js, timeout=10)

        if result == "followed":
            self._session.follows_used += 1
            self._daily_follows += 1
            logger.info(
                "follow_user: @%s (session=%d, daily=%d)",
                username,
                self._session.follows_used,
                self._daily_follows,
            )
            return True

        logger.warning("follow_user: follow button not found for @%s", username)
        return False

    def search_and_browse(self, query: str, graphql: GraphQLClient) -> list[dict]:
        """Search for users via GraphQL, increment search counter.

        Parameters
        ----------
        query : str
            Search query string.
        graphql : GraphQLClient
            GraphQL client for executing the search.

        Returns
        -------
        list[dict]
            List of user dicts from the search results.
        """
        if not self._session.can_search():
            logger.warning("search_and_browse: session search budget exhausted")
            return []

        self._delay()
        results = graphql.search_users(query)
        self._session.searches_used += 1
        logger.info(
            "search_and_browse: '%s' → %d results (session=%d)",
            query,
            len(results),
            self._session.searches_used,
        )
        return results

    def watch_reel(self, reel_url: str) -> bool:
        """Navigate to a reel, watch for a random time, increment counter.

        Parameters
        ----------
        reel_url : str
            Full Instagram reel URL.

        Returns
        -------
        bool
            True if the reel was watched, False otherwise.
        """
        if not self._session.can_view_reel():
            logger.warning("watch_reel: session reel budget exhausted")
            return False

        self._delay()
        self._cdp.navigate(reel_url, wait=3)

        # Random watch time between 3 and 15 seconds
        watch_time = random.uniform(3.0, 15.0)
        logger.debug("watch_reel: watching for %.2fs", watch_time)
        time.sleep(watch_time)

        self._session.reel_views_used += 1
        logger.info(
            "watch_reel: %s (%.1fs, session=%d)",
            reel_url,
            watch_time,
            self._session.reel_views_used,
        )
        return True

    # ------------------------------------------------------------------
    # Session loop
    # ------------------------------------------------------------------

    def run_session_loop(self, actions: list[Callable]) -> None:
        """Run a list of action callables until the session is exhausted.

        Each action is called with no arguments.  On success the loop
        continues; on exception a longer break (10–30 s) is taken before
        continuing.

        Parameters
        ----------
        actions : list[Callable]
            Callables to execute in order (cycled if the session is still
            active after one full pass).
        """
        if not actions:
            logger.warning("run_session_loop: no actions provided")
            return

        idx = 0
        while not self._session.is_exhausted():
            action = actions[idx % len(actions)]
            try:
                logger.debug("run_session_loop: executing action %d", idx)
                action()
            except Exception:
                # On error, take a longer break
                break_time = random.uniform(10.0, 30.0)
                logger.exception(
                    "run_session_loop: action %d failed — sleeping %.1fs",
                    idx,
                    break_time,
                )
                time.sleep(break_time)
            idx += 1

        logger.info(
            "run_session_loop: session exhausted after %d action calls",
            idx,
        )
