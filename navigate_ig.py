#!/usr/bin/env python3
"""Navigate all CDP ports to Instagram and verify login.

Three-tier recovery:
  1. Already on IG and logged in?  — verify and done.
  2. Have a real HTTP tab?          — navigate it to IG.
  3. No real HTTP tab?             — create a fresh tab via CDP browser-level Target.createTarget.

Uses the BROWSER-level WebSocket (from /json/version) for createTarget,
NOT a page-level WS — service workers and background pages can't issue
browser-level CDP commands.

Used by the Hermes watchdog cron and daemon recovery.
Runs on all configured CDP ports (9222, 9224, 9225).
"""
import json
import logging
import sys
import time
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("navigate_ig")

PORTS = [9222, 9224, 9225]


def _get_tab_list(base: str) -> list[dict]:
    """Fetch /json/list from the CDP endpoint."""
    resp = urllib.request.urlopen(f"{base}/json/list", timeout=5)
    return json.loads(resp.read())


def _get_browser_ws_url(base: str) -> str | None:
    """Fetch the browser-level WS URL from /json/version.

    Target.createTarget is a browser-level command and MUST be sent
    to the browser WebSocket endpoint, not a page/devtools endpoint.
    """
    try:
        resp = urllib.request.urlopen(f"{base}/json/version", timeout=5)
        data = json.loads(resp.read())
        return data.get("webSocketDebuggerUrl")
    except Exception as e:
        logger.warning(f"Failed to get browser WS URL: {e}")
        return None


def _verify_login(ws_url: str) -> bool:
    """Check if an IG tab is logged in via SVG presence."""
    import websocket as _ws
    try:
        ws = _ws.create_connection(ws_url, timeout=10, origin=None)
        check = {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": 'document.querySelector("svg[aria-label=Home]") ? "LOGGED_IN" : "NOT_LOGGED_IN"',
                "returnByValue": True,
            },
        }
        ws.send(json.dumps(check))
        raw = ws.recv()
        ws.close()
        data = json.loads(raw)
        status = data.get("result", {}).get("result", {}).get("value", "UNKNOWN")
        logger.info(f"Login check: {status}")
        return status == "LOGGED_IN"
    except Exception as e:
        logger.warning(f"Login check failed: {e}")
        return False


def _create_ig_tab_via_cdp(base: str, port: int) -> bool:
    """Open a new browser tab to IG via CDP's browser-level Target.createTarget.

    Uses the BROWSER WebSocket endpoint (from /json/version), NOT a page-level WS.
    Service workers, background pages, and iframes cannot issue browser-level CDP
    commands — this is the correct endpoint.

    Returns True if the new tab is confirmed logged in.
    """
    import websocket as _ws

    browser_ws = _get_browser_ws_url(base)
    if not browser_ws:
        logger.warning(f"Port {port}: no browser WS URL for createTarget")
        return False

    try:
        ws = _ws.create_connection(browser_ws, timeout=10, origin=None)
        nav = {
            "id": 1,
            "method": "Target.createTarget",
            "params": {"url": "https://www.instagram.com/", "newWindow": False},
        }
        ws.send(json.dumps(nav))
        raw = ws.recv()
        ws.close()

        result = json.loads(raw)
        target_id = result.get("result", {}).get("targetId")
        if not target_id:
            logger.warning(f"Port {port}: createTarget returned no targetId: {raw[:200]}")
            return False
        logger.info(f"Port {port}: created new tab targetId={target_id}")

        # Wait for page to load
        time.sleep(6)

        # Find the new IG tab and verify login
        tabs = _get_tab_list(base)
        ig = [t for t in tabs if "instagram.com" in t.get("url", "").lower()]
        if ig:
            logger.info(f"Port {port}: found IG tab, verifying login")
            return _verify_login(ig[0]["webSocketDebuggerUrl"])

        logger.warning(f"Port {port}: created tab but IG not found in URL list")
        return False
    except Exception as e:
        logger.warning(f"Port {port}: createTarget failed: {e}")
        return False


def _navigate_existing_tab(base: str, port: int, tab: dict) -> bool:
    """Navigate an existing real HTTP tab to instagram.com via Page.navigate.

    Returns True if navigation succeeded and login verified.
    """
    import websocket as _ws
    try:
        ws_url = tab["webSocketDebuggerUrl"]
        ws = _ws.create_connection(ws_url, timeout=10, origin=None)
        nav = {
            "id": 1,
            "method": "Page.navigate",
            "params": {"url": "https://www.instagram.com/"},
        }
        ws.send(json.dumps(nav))
        time.sleep(6)
        ws.close()

        # Find the IG tab and verify
        tabs = _get_tab_list(base)
        ig = [t for t in tabs if "instagram.com" in t.get("url", "").lower()]
        if ig:
            return _verify_login(ig[0]["webSocketDebuggerUrl"])
        return False
    except Exception as e:
        logger.warning(f"Port {port}: navigate existing tab failed: {e}")
        return False


def navigate_port(port: int) -> bool:
    """Ensure port has a logged-in IG tab. Three-tier recovery:

      1. Already have a verified logged-in IG tab → done.
      2. Have a real HTTP tab → navigate it to IG, verify.
      3. No real tabs → create a fresh tab via browser-level Target.createTarget.
    """
    base = f"http://localhost:{port}"
    try:
        tabs = _get_tab_list(base)
    except Exception as e:
        logger.warning(f"Port {port}: unreachable: {e}")
        return False

    # Tier 1: already have a verified IG tab
    ig_tabs = [t for t in tabs if "instagram.com" in t.get("url", "").lower()]
    if ig_tabs:
        logger.info(f"Port {port}: checking {len(ig_tabs)} existing IG tab(s)")
        # Verify each existing IG tab's login status — return True if any is logged in
        for ig_tab in ig_tabs:
            ws_url = ig_tab.get("webSocketDebuggerUrl")
            if ws_url and _verify_login(ws_url):
                logger.info(f"Port {port}: found verified logged-in IG tab")
                return True
        logger.warning(f"Port {port}: existing IG tabs exist but NOT logged in")

    # Tier 2: navigate an existing real HTTP tab to IG
    real_tabs = [
        t for t in tabs
        if t.get("url", "").startswith("http")
        and "chrome-extension" not in t.get("url", "")
        and "blob:" not in t.get("url", "")
        and t.get("type") == "page"
    ]
    # Broaden to include any page-type tab (even chrome:// pages)
    page_tabs = [t for t in tabs if t.get("type") == "page"]

    if real_tabs:
        logger.info(f"Port {port}: navigating real HTTP tab to IG")
        if _navigate_existing_tab(base, port, real_tabs[0]):
            return True
        logger.warning(f"Port {port}: Tier 2 failed, falling to Tier 3")
    elif page_tabs:
        # Try navigating a chrome:// page tab — might work for some protocols
        logger.info(f"Port {port}: navigating page tab to IG ({page_tabs[0].get('url','')[:60]})")
        if _navigate_existing_tab(base, port, page_tabs[0]):
            return True
        logger.warning(f"Port {port}: page tab navigate failed, falling to Tier 3")
    else:
        logger.warning(f"Port {port}: no page tabs at all")

    # Tier 3: create fresh tab via browser-level CDP
    logger.info(f"Port {port}: creating new IG tab via Target.createTarget")
    return _create_ig_tab_via_cdp(base, port)


def main():
    results = {}
    for port in PORTS:
        results[port] = navigate_port(port)
        time.sleep(1)

    success = sum(1 for v in results.values() if v)
    total = len(PORTS)
    logger.info(f"Result: {success}/{total} ports on IG")

    if success == total:
        print(f"OK: {success}/{total} ports on IG")
        return 0
    else:
        failed = [p for p, ok in results.items() if not ok]
        print(f"PARTIAL: {success}/{total} ports on IG -- ports {failed}")
        return 1


if __name__ == "__main__":
    sys.exit(main())