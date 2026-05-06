"""ContentEngager — organically engage with IG content.

For each content item:
1. Navigate to the URL
2. Dwell/watch (human-like)
3. Like the post
4. Save the post
5. Signal "interested" (through positive engagement signals)
6. Optionally add to a named collection
7. Log everything to DB
"""
from __future__ import annotations

import json as _json
import logging
import random
import time
from typing import Any

from igautomation.behavior.config import BehaviorConfig, SessionConfig
from igautomation.behavior.engine import BehaviorEngine
from igautomation.cdp.client import CDPClient
from igautomation.content.models import (
    ContentItem, ContentEngagementResult, EngagementAction,
    EngagementStatus, ContentType,
)
from igautomation.db.store import AsyncDatabaseStore

logger = logging.getLogger(__name__)


class ContentEngager:
    """Engage with Instagram content organically.

    Uses BehaviorEngine for rate limiting and timing.
    Performs like, save, interested-signal, and collection-assign actions.
    """

    def __init__(
        self,
        cdp: CDPClient,
        store: AsyncDatabaseStore,
        config: BehaviorConfig | None = None,
        session: SessionConfig | None = None,
    ) -> None:
        self._cdp = cdp
        self._store = store
        self._config = config or BehaviorConfig()
        self._session = session or self._config.new_session()
        self._engine = BehaviorEngine(cdp, self._config, self._session)

    def _delay(self) -> None:
        secs = self._config.action_delay()
        logger.debug("content delay: %.2fs", secs)
        time.sleep(secs)

    def _dwell(self) -> None:
        secs = self._config.read_dwell()
        logger.debug("content dwell: %.2fs", secs)
        time.sleep(secs)

    def engage_content(self, item: ContentItem) -> ContentEngagementResult:
        """Full organic engagement with a single content item.

        Steps: navigate → dwell → like → save → signal interest → collection.
        Each step respects session/daily budgets and adds human-like delays.
        """
        result = ContentEngagementResult(url=item.url)
        start = time.monotonic()

        try:
            # 1. Navigate and dwell
            self._delay()
            self._cdp.navigate(item.url, wait=3)
            self._dwell()

            # 2. Watch if it's a reel/clip (simulate viewing)
            if item.content_type in (ContentType.REEL, ContentType.VIDEO):
                watch_time = random.uniform(5.0, 20.0)
                logger.info("watching reel for %.1fs: %s", watch_time, item.url)
                time.sleep(watch_time)
                result.watch = EngagementStatus.DONE

            # 3. Like the post
            if self._engine.can_like():
                liked = self._like_via_js()
                result.like = EngagementStatus.DONE if liked else EngagementStatus.FAILED
            else:
                result.like = EngagementStatus.SKIPPED

            self._delay()

            # 4. Save the post
            saved = self._save_via_js()
            result.save = EngagementStatus.DONE if saved else EngagementStatus.FAILED

            self._delay()

            # 5. Signal "interested" — implicit through positive engagement
            # (watching + liking + saving = strong interest signal to IG algo)
            # Additionally, we can double-tap to like on reels
            if item.content_type in (ContentType.REEL, ContentType.VIDEO):
                self._double_tap_like()
            result.interested = EngagementStatus.DONE

            # 6. Add to collection if suggested
            if item.llm_collection_suggestion:
                collection_added = self._add_to_collection_via_js(item.llm_collection_suggestion)
                result.collection = item.llm_collection_suggestion
                result.collection_added = EngagementStatus.DONE if collection_added else EngagementStatus.FAILED

        except Exception as exc:
            result.error = str(exc)
            logger.exception("engage_content failed for %s: %s", item.url, exc)

        result.elapsed_seconds = time.monotonic() - start
        return result

    def _like_via_js(self) -> bool:
        """Like a post/reel using JavaScript click on the Like button."""
        js = """
        (function() {
            // Try svg aria-label="Like" (unfilled heart)
            var svg = document.querySelector('svg[aria-label="Like"]');
            if (svg) {
                var btn = svg.closest('button') || svg.closest('[role="button"]') || svg.parentElement;
                if (btn) { btn.click(); return 'liked'; }
            }
            // Try the Like text span inside a button
            var spans = document.querySelectorAll('span');
            for (var i = 0; i < spans.length; i++) {
                if (spans[i].textContent.trim() === 'Like') {
                    var parent = spans[i].closest('button') || spans[i].closest('[role="button"]');
                    if (parent) { parent.click(); return 'liked'; }
                }
            }
            return 'not_found';
        })()
        """
        result = self._cdp.evaluate(js, timeout=10)
        if result == "liked":
            self._session.likes_used += 1
            logger.info("liked post (session=%d)", self._session.likes_used)
            return True
        logger.warning("like button not found")
        return False

    def _double_tap_like(self) -> bool:
        """Double-tap on the media area to like (common on reels)."""
        js = """
        (function() {
            // Find the video or image element and double-click
            var media = document.querySelector('video') || document.querySelector('article img');
            if (media) {
                var rect = media.getBoundingClientRect();
                var evt = new MouseEvent('dblclick', {
                    bubbles: true, cancelable: true,
                    clientX: rect.left + rect.width / 2,
                    clientY: rect.top + rect.height / 2
                });
                media.dispatchEvent(evt);
                return 'double_tapped';
            }
            return 'no_media';
        })()
        """
        result = self._cdp.evaluate(js, timeout=10)
        if result == "double_tapped":
            logger.info("double-tap like sent")
            return True
        return False

    def _save_via_js(self) -> bool:
        """Save a post/reel using JavaScript click on the Save button."""
        js = """
        (function() {
            // Try the Save button (bookmark icon)
            var svg = document.querySelector('svg[aria-label="Save"]');
            if (svg) {
                var btn = svg.closest('button') || svg.closest('[role="button"]') || svg.parentElement;
                if (btn) { btn.click(); return 'saved'; }
            }
            // Try "Save" text
            var spans = document.querySelectorAll('span');
            for (var i = 0; i < spans.length; i++) {
                if (spans[i].textContent.trim() === 'Save') {
                    var parent = spans[i].closest('button') || spans[i].closest('[role="button"]');
                    if (parent) { parent.click(); return 'saved'; }
                }
            }
            return 'not_found';
        })()
        """
        result = self._cdp.evaluate(js, timeout=10)
        if result == "saved":
            logger.info("saved post")
            return True
        logger.warning("save button not found")
        return False

    def _add_to_collection_via_js(self, collection_name: str) -> bool:
        """Save to a specific collection via the Save dialog.

        If already saved, clicks the saved bookmark to open the collection dialog,
        then selects or creates the named collection.
        """
        safe_name = _json.dumps(collection_name)
        js = f"""
        (function() {{
            // If already saved, click the Remove/Saved button to open collections
            var savedSvg = document.querySelector('svg[aria-label="Remove"]');
            if (savedSvg) {{
                var btn = savedSvg.closest('button') || savedSvg.closest('[role="button"]') || savedSvg.parentElement;
                if (btn) {{
                    btn.click();
                    return new Promise(function(resolve) {{
                        setTimeout(function() {{
                            var spans = document.querySelectorAll('span, div[role="button"]');
                            for (var i = 0; i < spans.length; i++) {{
                                if (spans[i].textContent.trim() === {safe_name}) {{
                                    spans[i].click();
                                    resolve('collection_added');
                                    return;
                                }}
                            }}
                            resolve('collection_not_found');
                        }}, 1500);
                    }});
                }}
            }}
            // If not yet saved, click Save first, then try collection
            var saveSvg = document.querySelector('svg[aria-label="Save"]');
            if (saveSvg) {{
                var btn = saveSvg.closest('button') || saveSvg.closest('[role="button"]') || saveSvg.parentElement;
                if (btn) {{
                    btn.click();
                    // After saving, long-press or click the saved icon to add to collection
                    return new Promise(function(resolve) {{
                        setTimeout(function() {{
                            var savedSvg2 = document.querySelector('svg[aria-label="Remove"]');
                            if (savedSvg2) {{
                                var btn2 = savedSvg2.closest('button') || savedSvg2.closest('[role="button"]') || savedSvg2.parentElement;
                                if (btn2) {{
                                    btn2.click();
                                    setTimeout(function() {{
                                        var spans = document.querySelectorAll('span, div[role="button"]');
                                        for (var i = 0; i < spans.length; i++) {{
                                            if (spans[i].textContent.trim() === {safe_name}) {{
                                                spans[i].click();
                                                resolve('collection_added');
                                                return;
                                            }}
                                        }}
                                        resolve('collection_not_found');
                                    }}, 1500);
                                }} else {{
                                    resolve('no_saved_btn');
                                }}
                            }} else {{
                                resolve('no_saved_icon');
                            }}
                        }}, 1000);
                    }});
                }}
            }}
            return 'no_save_button';
        }})()
        """
        result = self._cdp.evaluate(js, timeout=15)
        if result in ("collection_added", "saved"):
            logger.info("added to collection \'%s\'", collection_name)
            return True
        logger.warning("could not add to collection \'%s\': %s", collection_name, result)
        return False

    async def log_engagement(
        self, item: ContentItem, result: ContentEngagementResult, session_id: str | None = None
    ) -> None:
        """Log engagement results to the database."""
        # Log each action
        for action_field, action_type in [
            ("like", "content_like"),
            ("save", "content_save"),
            ("interested", "content_interested"),
            ("watch", "content_watch"),
            ("collection_added", "content_collection_add"),
        ]:
            status = getattr(result, action_field)
            if status != EngagementStatus.PENDING:
                # Get or create the content item in DB
                db_item = await self._store.get_content_item_by_url(item.url)
                if db_item:
                    await self._store.log_content_engagement(
                        content_item_id=db_item["id"],
                        action_type=action_type,
                        status=status.value,
                        detail=f"{item.url}|{result.collection or ''}",
                        session_id=session_id,
                    )
