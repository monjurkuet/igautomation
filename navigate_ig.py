#!/usr/bin/env python3
"""Navigate all CDP ports to Instagram and verify login.

Used by the systemd service startup and the Hermes watchdog cron.
Runs on all configured CDP ports (9222, 9224, 9225).
"""
import json
import logging
import sys
import urllib.request
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("navigate_ig")

PORTS = [9222, 9224, 9225]


def navigate_port(port: int) -> bool:
    """Navigate first real browser tab on port to instagram.com. Returns True if logged in."""
    base = f"http://localhost:{port}"
    try:
        resp = urllib.request.urlopen(f"{base}/json/list", timeout=5)
        tabs = json.loads(resp.read())
        real = [
            t for t in tabs
            if t.get("url", "").startswith("http")
            and "chrome-extension" not in t.get("url", "")
            and "blob:" not in t.get("url", "")
        ]
        if not real:
            logger.warning("Port %d: no real tabs to navigate", port)
            return False

        # Check if already on IG
        ig_tabs = [t for t in tabs if "instagram.com" in t.get("url", "").lower()]
        if ig_tabs:
            logger.info("Port %d: already on IG (%d tabs)", port, len(ig_tabs))
            return True

        # Navigate first tab to IG
        import websocket as _ws
        ws_url = real[0]["webSocketDebuggerUrl"]
        ws = _ws.create_connection(ws_url, timeout=10, origin=None)
        # Navigate
        nav = {
            "id": 1,
            "method": "Page.navigate",
            "params": {"url": "https://www.instagram.com/"},
        }
        ws.send(json.dumps(nav))
        time.sleep(6)
        ws.close()

        # Verify login
        try:
            resp2 = urllib.request.urlopen(f"{base}/json/list", timeout=5)
            tabs2 = json.loads(resp2.read())
            ig = [t for t in tabs2 if "instagram.com" in t.get("url", "").lower()]
            if ig:
                ws2 = _ws.create_connection(ig[0]["webSocketDebuggerUrl"], timeout=10, origin=None)
                check = {
                    "id": 2,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": 'document.querySelector("svg[aria-label=Home]") ? "LOGGED_IN" : "NOT_LOGGED_IN"',
                        "returnByValue": True,
                    },
                }
                ws2.send(json.dumps(check))
                raw = ws2.recv()
                ws2.close()
                data = json.loads(raw)
                status = data.get("result", {}).get("result", {}).get("value", "UNKNOWN")
                if status == "LOGGED_IN":
                    logger.info("Port %d: login OK", port)
                    return True
                else:
                    logger.warning("Port %d: navigated but NOT logged in", port)
                    return False
        except Exception as e:
            logger.warning("Port %d: login check failed: %s", port, e)

        return False
    except Exception as e:
        logger.warning("Port %d: navigation failed: %s", port, e)
        return False


def main():
    results = {}
    for port in PORTS:
        results[port] = navigate_port(port)
        time.sleep(1)

    success = sum(1 for v in results.values() if v)
    total = len(PORTS)
    logger.info("Result: %d/%d ports on IG", success, total)

    if success == total:
        print(f"OK: {success}/{total} ports on IG")
        return 0
    else:
        print(f"PARTIAL: {success}/{total} ports on IG — ports {[p for p, ok in results.items() if not ok]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
