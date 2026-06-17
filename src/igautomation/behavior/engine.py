"""BehaviorEngine — wraps CDPClient with human-like timing and session budgets.

Every action method:
1. Checks session and daily budgets before proceeding.
2. Adds a randomised delay (``_delay``) between actions.
3. Adds a read/dwell delay (``_dwell``) to simulate human reading.
4. Increments the relevant session and daily counters on success.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Callable

from igautomation.behavior.config import BehaviorConfig, SessionConfig
from igautomation.cdp.client import CDPClient, SKIP_USERNAMES
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
        self._daily_unfollows: int = 0
        self._daily_story_views: int = 0
        self._daily_comments: int = 0

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

    def browse_feed(self, max_scrolls: int = 15) -> dict:
        """Scroll the main feed, extracting post URLs, usernames, and metadata.

        Returns dict: posts (list of dicts with url/username/likes),
        usernames (list of str), scrolls_done (int)
        """
        self._delay()
        all_posts: list[dict] = []
        seen_urls: set[str] = set()
        all_usernames: set[str] = set()
        scrolls_done = 0

        for _ in range(max_scrolls):
            if self._session.is_exhausted():
                break

            extract_js = """(function() {
                var results = [];
                var links = document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]');
                links.forEach(function(a) {
                    var href = a.getAttribute('href') || '';
                    var username = '';
                    var parent = a.closest('article, div[role="button"]');
                    if (parent) {
                        var userLinks = parent.querySelectorAll('a[href*="instagram.com/"]');
                        if (userLinks.length > 0) {
                            var m = userLinks[0].getAttribute('href').match(/instagram\\.com\\/([^/]+)/);
                            if (m) username = m[1];
                        }
                    }
                    results.push({url: 'https://www.instagram.com' + href, username: username});
                });
                return JSON.stringify(results);
            })()"""
            raw = self._cdp.evaluate(extract_js, timeout=10)
            if raw:
                try:
                    for p in json.loads(raw):
                        url = p.get("url", "")
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            all_posts.append(p)
                            uname = p.get("username", "")
                            if uname and uname not in SKIP_USERNAMES:
                                all_usernames.add(uname)
                except (json.JSONDecodeError, TypeError):
                    pass

            self._cdp.evaluate("window.scrollBy(0, window.innerHeight * 0.8)", timeout=5)
            time.sleep(self._config.scroll_delay())
            scrolls_done += 1

        logger.info("browse_feed: %d posts, %d usernames, %d scrolls",
                     len(all_posts), len(all_usernames), scrolls_done)
        return {"posts": all_posts, "usernames": list(all_usernames), "scrolls_done": scrolls_done}

    def browse_reels(self, max_reels: int = 20) -> dict:
        """Swipe through the Reels tab, extracting reel URLs and metadata.

        Returns dict: reels (list of dicts), scrolls_done (int)
        """
        self._delay()
        self._cdp.navigate("https://www.instagram.com/reels/", wait=4)
        time.sleep(2)

        all_reels: list[dict] = []
        seen_urls: set[str] = set()
        scrolls_done = 0

        for _ in range(max_reels):
            if self._session.is_exhausted():
                break
            if not self._session.can_view_reel():
                break

            extract_js = """(function() {
                var r = {};
                var links = document.querySelectorAll('a[href*="/reel/"]');
                if (links.length > 0) {
                    r.url = 'https://www.instagram.com' + links[links.length - 1].getAttribute('href');
                }
                var userLinks = document.querySelectorAll('a[href*="instagram.com/"]');
                for (var i = 0; i < userLinks.length; i++) {
                    var href = userLinks[i].getAttribute('href') || '';
                    var m = href.match(/instagram\\.com\\/([^/?/]+)/);
                    if (m && m[1] && !['reel','reels','p','explore','direct'].includes(m[1])) {
                        r.username = m[1]; break;
                    }
                }
                var spans = document.querySelectorAll('span[dir="auto"]');
                for (var i = 0; i < spans.length; i++) {
                    var t = spans[i].textContent.trim();
                    if (t.length > 20 && t.length < 500) { r.caption = t; break; }
                }
                return JSON.stringify(r);
            })()"""
            raw = self._cdp.evaluate(extract_js, timeout=10)
            if raw:
                try:
                    reel = json.loads(raw)
                    url = reel.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        all_reels.append(reel)
                except (json.JSONDecodeError, TypeError):
                    pass

            watch_time = random.uniform(3.0, 12.0)
            time.sleep(watch_time)
            self._session.reel_views_used += 1

            self._cdp.evaluate(
                "window.scrollBy(0, window.innerHeight);", timeout=5,
            )
            time.sleep(random.uniform(1.0, 3.0))
            scrolls_done += 1

        logger.info("browse_reels: %d reels, %d swipes", len(all_reels), scrolls_done)
        return {"reels": all_reels, "scrolls_done": scrolls_done}

    def browse_explore(self, max_scrolls: int = 10) -> dict:
        """Browse the Explore tab for trending content.

        Returns dict: posts (list of dicts), usernames (list of str), scrolls_done (int)
        """
        self._delay()
        self._cdp.navigate("https://www.instagram.com/explore/", wait=5)
        time.sleep(2)

        all_posts: list[dict] = []
        seen_urls: set[str] = set()
        all_usernames: set[str] = set()
        scrolls_done = 0

        for _ in range(max_scrolls):
            if self._session.is_exhausted():
                break

            extract_js = """(function() {
                var results = [];
                var links = document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"], a[href*="/tv/"]');
                links.forEach(function(a) {
                    var href = a.getAttribute('href') || '';
                    var username = '';
                    var parent = a.closest('div');
                    if (parent) {
                        var userLinks = parent.querySelectorAll('a[href*="instagram.com/"]');
                        if (userLinks.length > 0) {
                            var m = userLinks[0].getAttribute('href').match(/instagram\\.com\\/([^/]+)/);
                            if (m) username = m[1];
                        }
                    }
                    results.push({url: 'https://www.instagram.com' + href, username: username});
                });
                return JSON.stringify(results);
            })()"""
            raw = self._cdp.evaluate(extract_js, timeout=10)
            if raw:
                try:
                    for p in json.loads(raw):
                        url = p.get("url", "")
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            all_posts.append(p)
                            uname = p.get("username", "")
                            if uname and uname not in SKIP_USERNAMES:
                                all_usernames.add(uname)
                except (json.JSONDecodeError, TypeError):
                    pass

            self._cdp.evaluate("window.scrollBy(0, window.innerHeight * 0.8)", timeout=5)
            time.sleep(self._config.scroll_delay())
            scrolls_done += 1

        logger.info("browse_explore: %d posts, %d usernames, %d scrolls",
                     len(all_posts), len(all_usernames), scrolls_done)
        return {"posts": all_posts, "usernames": list(all_usernames), "scrolls_done": scrolls_done}

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

    def view_stories(self, username: str, max_stories: int = 5) -> int:
        """Navigate to a profile, open stories, watch them passively.

        Parameters
        ----------
        username : str
            Instagram username (without @).
        max_stories : int
            Maximum number of story segments to view.

        Returns
        -------
        int
            Number of story segments viewed.
        """
        if not self._session.can_view_story():
            logger.warning("view_stories: budget exhausted for @%s", username)
            return 0

        self._delay()
        url = f"https://www.instagram.com/{username}/"
        self._cdp.navigate(url, wait=4)

        # Click the story ring (circular element at top of profile with rainbow border)
        click_js = """(function() {
            // Story ring: canvas or link element near profile header
            var rings = document.querySelectorAll('canvas, a[role="link"]');
            for (var i = 0; i < rings.length; i++) {
                var r = rings[i];
                var rect = r.getBoundingClientRect();
                // Story ring is near the top, small square-ish
                if (rect.top < 200 && rect.width > 50 && rect.width < 150
                    && rect.height > 50 && rect.height < 150) {
                    r.click();
                    return 'clicked';
                }
            }
            // Fallback: look for any element with "story" in class/href
            var links = document.querySelectorAll('[class*="story"], a[href*="/stories/"]');
            if (links.length > 0) { links[0].click(); return 'clicked'; }
            return 'no_story';
        })()"""

        result = self._cdp.evaluate(click_js, timeout=10)
        if result != "clicked":
            logger.info("view_stories: @%s has no active stories", username)
            return 0

        # Wait for story viewer to open
        time.sleep(2)

        viewed = 0
        for _ in range(max_stories):
            if not self._session.can_view_story():
                break

            # Watch current story segment for 3-8 seconds
            watch_time = random.uniform(3.0, 8.0)
            time.sleep(watch_time)
            viewed += 1
            self._session.story_views_used += 1
            self._daily_story_views += 1

            # Advance to next story (click right side of screen or press Right arrow)
            advance_js = """(function() {
                // Try clicking the right-side navigation arrow
                var btns = document.querySelectorAll('button, div[role="button"]');
                for (var i = 0; i < btns.length; i++) {
                    var b = btns[i];
                    var rect = b.getBoundingClientRect();
                    if (rect.right > window.innerWidth * 0.7 && rect.top < window.innerHeight * 0.5) {
                        b.click();
                        return 'advanced';
                    }
                }
                return 'end';
            })()"""
            adv_result = self._cdp.evaluate(advance_js, timeout=5)
            if adv_result != "advanced":
                break

            time.sleep(1)

        # Close story viewer
        close_js = """(function() {
            var closeBtns = document.querySelectorAll('button');
            for (var i = 0; i < closeBtns.length; i++) {
                if (closeBtns[i].querySelector('svg[aria-label="Close"]')) {
                    closeBtns[i].click(); return 'closed';
                }
            }
            // Fallback: press Escape
            document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', keyCode: 27}));
            return 'escaped';
        })()"""
        self._cdp.evaluate(close_js, timeout=5)
        time.sleep(1)

        logger.info(
            "view_stories: @%s — %d segments (session=%d, daily=%d)",
            username, viewed, self._session.story_views_used, self._daily_story_views,
        )
        return viewed

    def unfollow_user(self, username: str) -> bool:
        """Navigate to a profile, click Following, confirm Unfollow.

        Parameters
        ----------
        username : str
            Instagram username (without @).

        Returns
        -------
        bool
            True if unfollowed, False otherwise.
        """
        if not self._session.can_unfollow():
            logger.warning("unfollow_user: budget exhausted for @%s", username)
            return False

        self._delay()
        url = f"https://www.instagram.com/{username}/"
        self._cdp.navigate(url, wait=3)
        self._dwell()

        # Click "Following" button then confirm "Unfollow" in the popup
        unfollow_js = """(function() {
            var btns = document.querySelectorAll('button');
            for (var i = 0; i < btns.length; i++) {
                if (btns[i].textContent.trim() === 'Following') {
                    btns[i].click();
                    return 'opened_dialog';
                }
            }
            // Already not following — button says "Follow"
            for (var i = 0; i < btns.length; i++) {
                if (btns[i].textContent.trim() === 'Follow') {
                    return 'not_following';
                }
            }
            return 'not_found';
        })()"""

        result = self._cdp.evaluate(unfollow_js, timeout=10)
        if result == "not_following":
            logger.info("unfollow_user: @%s — already not following", username)
            return False
        if result != "opened_dialog":
            logger.warning("unfollow_user: @%s — Following button not found", username)
            return False

        # Wait for confirmation dialog, click "Unfollow"
        time.sleep(1)
        confirm_js = """(function() {
            var btns = document.querySelectorAll('button');
            for (var i = 0; i < btns.length; i++) {
                if (btns[i].textContent.trim() === 'Unfollow') {
                    btns[i].click();
                    return 'unfollowed';
                }
            }
            return 'not_found';
        })()"""

        result = self._cdp.evaluate(confirm_js, timeout=10)
        if result == "unfollowed":
            self._session.unfollows_used += 1
            self._daily_unfollows += 1
            logger.info(
                "unfollow_user: @%s (session=%d, daily=%d)",
                username, self._session.unfollows_used, self._daily_unfollows,
            )
            return True

        logger.warning("unfollow_user: @%s — Unfollow confirm not found", username)
        return False

    def comment_on_post(self, post_url: str, comment_text: str) -> bool:
        """Navigate to a post, type a comment, submit it.

        Parameters
        ----------
        post_url : str
            Full Instagram post URL.
        comment_text : str
            Comment text to post.

        Returns
        -------
        bool
            True if comment was posted, False otherwise.
        """
        if not self._session.can_comment():
            logger.warning("comment_on_post: budget exhausted")
            return False

        self._delay()
        self._cdp.navigate(post_url, wait=4)
        self._dwell()

        # Click comment textarea to focus it
        focus_js = """(function() {
            var ta = document.querySelector('textarea[aria-label*="comment"], textarea[placeholder*="comment"], form textarea');
            if (ta) { ta.focus(); ta.click(); return 'focused'; }
            return 'not_found';
        })()"""

        result = self._cdp.evaluate(focus_js, timeout=10)
        if result != "focused":
            logger.warning("comment_on_post: textarea not found for %s", post_url)
            return False

        time.sleep(1)

        # Type comment using execCommand (simulates real keyboard input)
        import json as _json
        safe_text = _json.dumps(comment_text)  # JSON-escape for JS string
        type_js = f"""(function() {{
            var ta = document.querySelector('textarea[aria-label*="comment"], textarea[placeholder*="comment"], form textarea');
            if (!ta) return 'not_found';
            ta.focus();
            ta.value = '';
            document.execCommand('insertText', false, {safe_text});
            ta.dispatchEvent(new Event('input', {{bubbles: true}}));
            return 'typed';
        }})()"""

        result = self._cdp.evaluate(type_js, timeout=10)
        if result != "typed":
            logger.warning("comment_on_post: failed to type for %s", post_url)
            return False

        # Wait briefly before submitting
        time.sleep(random.uniform(1.0, 3.0))

        # Press Enter or click Post button
        post_js = """(function() {
            // Try pressing Enter in the textarea
            var ta = document.querySelector('textarea[aria-label*="comment"], textarea[placeholder*="comment"], form textarea');
            if (ta) {
                ta.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', keyCode: 13, bubbles: true}));
            }
            // Also try clicking "Post" button
            var btns = document.querySelectorAll('button');
            for (var i = 0; i < btns.length; i++) {
                if (btns[i].textContent.trim() === 'Post') {
                    btns[i].click();
                    return 'posted';
                }
            }
            return 'enter_sent';
        })()"""

        self._cdp.evaluate(post_js, timeout=10)
        time.sleep(2)

        self._session.comments_used += 1
        self._daily_comments += 1
        logger.info(
            "comment_on_post: %s — '%s' (session=%d, daily=%d)",
            post_url, comment_text[:30], self._session.comments_used, self._daily_comments,
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
