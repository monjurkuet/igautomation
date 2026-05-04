"""Chrome DevTools tab discovery via the HTTP /json endpoint.

Provides helpers to list open Chrome tabs, find Instagram tabs, and
return their WebSocket debugger URLs for use with :class:`CDPClient`.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Default Chrome remote-debugging base URL.
DEFAULT_BASE_URL = "http://localhost:9224"


class TabDiscovery:
    """Discover Chrome tabs via the remote-debugging HTTP endpoint.

    Chrome exposes a simple JSON API at ``http://localhost:<port>/json``
    when launched with ``--remote-debugging-port=<port>``.  This class
    wraps that API with convenience methods for finding Instagram tabs.
    """

    @staticmethod
    def list_tabs(base_url: str = DEFAULT_BASE_URL) -> list[dict[str, Any]]:
        """Return all page-type tabs from Chrome's ``/json`` endpoint.

        Tabs with ``"type": "iframe"`` are filtered out because they are
        sub-frames, not top-level tabs that accept CDP connections.

        Args:
            base_url: Chrome remote-debugging HTTP base URL
                (default ``http://localhost:9224``).

        Returns:
            List of tab info dicts.  Each dict contains at least:
            ``id``, ``title``, ``url``, ``webSocketDebuggerUrl``, and
            ``type``.
        """
        try:
            resp = requests.get(f"{base_url}/json", timeout=5)
            resp.raise_for_status()
            tabs: list[dict[str, Any]] = resp.json()
        except requests.RequestException:
            logger.exception("Failed to list Chrome tabs from %s", base_url)
            return []

        # Filter out iframe entries — they can't be directly connected to.
        page_tabs = [t for t in tabs if t.get("type") != "iframe"]
        logger.debug(
            "Found %d tabs (%d total, %d iframes filtered)",
            len(page_tabs),
            len(tabs),
            len(tabs) - len(page_tabs),
        )
        return page_tabs

    @staticmethod
    def find_ig_tab(
        base_url: str = DEFAULT_BASE_URL,
        url_pattern: str = "instagram.com",
    ) -> dict[str, Any] | None:
        """Find the first Chrome tab whose URL matches *url_pattern*.

        Args:
            base_url: Chrome remote-debugging HTTP base URL.
            url_pattern: Substring to search for in tab URLs
                (default ``"instagram.com"``).

        Returns:
            The matching tab dict, or ``None`` if no match was found.
        """
        tabs = TabDiscovery.list_tabs(base_url)
        for tab in tabs:
            if url_pattern in tab.get("url", ""):
                logger.info(
                    "Found matching tab: %s (%s)",
                    tab.get("title", "?")[:60],
                    tab.get("url", ""),
                )
                return tab

        logger.info("No tab matching '%s' found", url_pattern)
        return None

    @staticmethod
    def get_ig_tabs(base_url: str = DEFAULT_BASE_URL) -> list[dict[str, Any]]:
        """Return all Chrome tabs whose URL contains ``instagram.com``.

        Args:
            base_url: Chrome remote-debugging HTTP base URL.

        Returns:
            List of tab info dicts for Instagram tabs (may be empty).
        """
        tabs = TabDiscovery.list_tabs(base_url)
        ig_tabs = [t for t in tabs if "instagram.com" in t.get("url", "")]
        logger.info("Found %d Instagram tab(s)", len(ig_tabs))
        return ig_tabs
