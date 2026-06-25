"""Chrome DevTools Protocol client for browser automation.

Uses short-lived WebSocket connections per CDP command — this is the pattern
that works reliably with Chrome's CDP. Long-lived connections get killed by
Chrome, so each method opens a fresh connection, sends its command, reads
until the matching response arrives, then closes.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import websocket

logger = logging.getLogger(__name__)

# Instagram navigation paths that are NOT user profiles.
SKIP_USERNAMES: set[str] = {
    "explore",
    "reels",
    "direct",
    "accounts",
    "stories",
    "p",
    "reel",
    "tv",
    "shop",
    "channels",
    "popular",
    "locations",
    "directory",
    "help",
    "legal",
    "about",
    "press",
    "api",
    "developer",
    "blog",
    "faq",
    "terms",
    "privacy",
    "login",
    "signup",
    "accountscenter",
    "your_activity",
    "notifications",
    "edit",
    "settings",
    "download",
    "emails",
    "contact",
    "support",
    "integrity",
    "transparency",
}

# Regex for valid Instagram usernames (profile path segments).
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._]{2,30}$")


class CDPClient:
    """Low-level CDP client that communicates with Chrome over WebSocket.

    Every public method opens a fresh WebSocket connection, sends a single
    CDP command, reads responses until the matching ``id`` is found, then
    closes the socket.  This avoids the long-lived-connection reliability
    issues that Chrome's CDP implementation is known for.

    The ``origin`` header *must* be ``chrome://inspect`` to satisfy Chrome's
    ``--remote-allow-origins`` restriction; otherwise the handshake is
    rejected with HTTP 403.
    """

    def __init__(self) -> None:
        self._ws_url: str | None = None
        self._origin: str | None = None  # None = no Origin header sent
        self._cmd_id: int = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        """Return the next sequential command ID."""
        self._cmd_id += 1
        return self._cmd_id

    def _open_ws(self, ws_url: str | None = None) -> websocket.WebSocket:
        """Open a fresh WebSocket connection to Chrome.

        Args:
            ws_url: Override the stored URL (useful for one-shot calls).

        Returns:
            A connected :class:`websocket.WebSocket` instance.
        """
        url = ws_url or self._ws_url
        if not url:
            raise RuntimeError("No WebSocket URL configured — call connect() first")

        for attempt in range(3):
            try:
                logger.debug(
                    "Opening WebSocket to %s (origin=%s, attempt=%d)",
                    url,
                    self._origin,
                    attempt + 1,
                )
                ws = websocket.WebSocket()
                ws.settimeout(30)
                ws.connect(url, origin=self._origin)
                return ws
            except Exception:
                if attempt < 2:
                    delay = 0.5 * (2**attempt)  # 0.5s, 1s
                    logger.warning(
                        "WebSocket connect failed (attempt %d), retrying in %.1fs",
                        attempt + 1,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    raise

    def _send_and_read(
        self,
        ws: websocket.WebSocket,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 20,
    ) -> dict[str, Any] | None:
        """Send a CDP command and read responses until the matching ID arrives.

        Args:
            ws: Open WebSocket connection.
            method: CDP method name (e.g. ``"Runtime.evaluate"``).
            params: Optional parameters dict.
            timeout: Seconds to wait for the matching response.

        Returns:
            The parsed CDP response dict, or ``None`` on timeout / error.
        """
        cmd_id = self._next_id()
        payload: dict[str, Any] = {"id": cmd_id, "method": method}
        if params:
            payload["params"] = params

        ws.settimeout(timeout)
        ws.send(json.dumps(payload))
        logger.debug("Sent CDP command id=%d method=%s", cmd_id, method)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ws.settimeout(remaining)
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                logger.warning("Timeout waiting for CDP response id=%d", cmd_id)
                break
            except websocket.WebSocketConnectionClosedException:
                logger.warning("WebSocket closed while waiting for CDP response id=%d", cmd_id)
                break

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("Skipping non-JSON CDP message: %s", raw[:200])
                continue

            if msg.get("id") == cmd_id:
                logger.debug("Received CDP response id=%d", cmd_id)
                return msg

            # Not our response — could be an event; skip it.
            logger.debug("Skipping CDP event (looking for id=%d): %s", cmd_id, str(msg)[:200])
            continue

        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def connect(self, ws_url: str, origin: str = "chrome://inspect") -> None:
        """Store the WebSocket URL for subsequent commands.

        The actual connection is opened per-call (short-lived pattern).
        The ``origin`` value defaults to ``chrome://inspect`` which is
        required to bypass Chrome's ``--remote-allow-origins`` check.

        Args:
            ws_url: WebSocket debugger URL (from ``/json`` endpoint).
            origin: Value for the HTTP ``Origin`` header during WS
                handshake.  **Do not change** unless you know Chrome is
                configured with a different ``--remote-allow-origins``.
        """
        self._ws_url = ws_url
        self._origin = origin
        logger.info("CDPClient configured for %s", ws_url)

    def evaluate(self, js: str, timeout: float = 20) -> str | None:
        """Execute JavaScript in the page context via ``Runtime.evaluate``.

        Opens a fresh WebSocket, sends the command with
        ``returnByValue=True`` and ``awaitPromise=True``, then closes.

        Args:
            js: JavaScript expression to evaluate.
            timeout: Seconds to wait for the result.

        Returns:
            The string representation of the result's ``value`` field,
            or ``None`` if evaluation failed / timed out.
        """
        ws = self._open_ws()
        try:
            resp = self._send_and_read(
                ws,
                "Runtime.evaluate",
                params={
                    "expression": js,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
                timeout=timeout,
            )
            if resp is None:
                return None

            result = resp.get("result", {}).get("result", {})
            if "exceptionDetails" in resp.get("result", {}):
                detail = resp["result"]["exceptionDetails"]
                logger.error(
                    "JS evaluation exception: %s",
                    detail.get("text", detail),
                )
                return None

            value = result.get("value")
            if value is None:
                # Could be undefined or a non-serialisable value.
                return None
            return str(value)
        except Exception:
            logger.exception("Error during evaluate()")
            return None
        finally:
            self._safe_close(ws)

    def navigate(self, url: str, wait: float = 4) -> dict | None:
        """Navigate the page to *url* via ``Page.navigate``.

        After sending the navigation command the method sleeps for *wait*
        seconds to allow the page to begin loading before the WebSocket
        is closed.

        Args:
            url: Target URL.
            wait: Seconds to sleep after issuing the navigate command.

        Returns:
            The CDP response dict (contains ``frameId`` and optionally
            ``loaderId``), or ``None`` on failure.
        """
        ws = self._open_ws()
        try:
            resp = self._send_and_read(
                ws,
                "Page.navigate",
                params={"url": url},
                timeout=15,
            )
            if resp and "result" in resp:
                logger.info(
                    "Navigated to %s (frameId=%s)",
                    url,
                    resp["result"].get("frameId", "?"),
                )

            # Give the page time to start loading / rendering.
            time.sleep(wait)
            return resp
        except Exception:
            logger.exception("Error during navigate(%s)", url)
            return None
        finally:
            self._safe_close(ws)

    def scroll(self, max_scrolls: int = 10, delay: float = 2.0) -> list[str]:
        """Scroll the page down multiple times, collecting Instagram profile links.

        After each scroll the method evaluates a JS snippet that scrapes
        all ``<a href="/…">`` links whose single path segment looks like a
        valid Instagram username.  Known non-profile paths (explore, reels,
        etc.) are filtered out via :data:`SKIP_USERNAMES`.

        Each scroll+scrape cycle opens a fresh WebSocket connection.

        Args:
            max_scrolls: Maximum number of scroll operations.
            delay: Seconds to wait after each scroll for new content
                to load.

        Returns:
            Deduplicated list of usernames found across all scrolls,
            in order of first appearance.
        """
        seen: set[str] = set()
        usernames: list[str] = []

        js_scroll = "window.scrollBy(0, document.body.scrollHeight)"
        js_collect = """
        (function() {
            var links = document.querySelectorAll('a[href]');
            var out = [];
            links.forEach(function(a) {
                var m = a.getAttribute('href');
                if (m) out.push(m);
            });
            return JSON.stringify(out);
        })()
        """

        for i in range(max_scrolls):
            # --- scroll ---
            self.evaluate(js_scroll, timeout=10)
            time.sleep(delay)

            # --- collect links ---
            raw = self.evaluate(js_collect, timeout=15)
            if not raw:
                logger.debug("Scroll %d: no links collected", i + 1)
                continue

            try:
                hrefs: list[str] = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Scroll %d: failed to parse link JSON", i + 1)
                continue

            new_count = 0
            for href in hrefs:
                # Normalise: strip leading/trailing slashes, keep only the
                # first path segment (e.g. "/username/" → "username").
                path = href.strip("/")
                if "/" in path:
                    path = path.split("/")[0]

                if path in SKIP_USERNAMES:
                    continue
                if not _USERNAME_RE.match(path):
                    continue
                if path in seen:
                    continue

                seen.add(path)
                usernames.append(path)
                new_count += 1

            logger.info(
                "Scroll %d/%d: found %d new usernames (%d total)",
                i + 1,
                max_scrolls,
                new_count,
                len(usernames),
            )

            # Stop early if the page isn't producing new profiles any more.
            if new_count == 0 and i >= 2:
                logger.info("No new usernames found — stopping scroll early")
                break

        return usernames

    def click_see_all(self) -> bool:
        """Find and click an element containing the text ``"See all"``.

        Instagram uses ``"See all"`` buttons/links to expand suggested
        accounts lists.  This method searches the DOM for any element
        whose trimmed text content matches and simulates a click.

        Returns:
            ``True`` if an element was found and clicked, ``False``
            otherwise.
        """
        js = """
        (function() {
            var els = document.querySelectorAll(
                'span, a, button, div'
            );
            for (var i = 0; i < els.length; i++) {
                var t = els[i].textContent.trim();
                if (t === 'See all') {
                    els[i].click();
                    return 'clicked';
                }
            }
            return 'not_found';
        })()
        """
        result = self.evaluate(js, timeout=10)
        if result == "clicked":
            logger.info("Clicked 'See all'")
            return True
        logger.warning("'See all' element not found")
        return False

    def close(self) -> None:
        """Close any stored WebSocket reference.

        Because each method uses its own short-lived connection, this is
        effectively a no-op — but it is kept for API completeness and in
        case a future refactor introduces a persistent connection.
        """
        self._ws_url = ""
        logger.debug("CDPClient.close() called — URL cleared")

    # ------------------------------------------------------------------
    # Private utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_close(ws: websocket.WebSocket) -> None:
        """Close a WebSocket, silently ignoring errors."""
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
