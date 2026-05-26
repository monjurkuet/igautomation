"""Account prober — discover which IG account is logged in on a CDP port.

Connects to an IG tab via CDP, reads ds_user_id from cookies,
then resolves user_id → username via IG's internal API.

Uses the proven approach from the multi-account-plan:
1. Find IG tab on the port
2. Read ds_user_id + csrftoken from document.cookie
3. Call /api/v1/users/{uid}/info/ to get username
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from igautomation.cdp.client import CDPClient
from igautomation.cdp.discovery import TabDiscovery

logger = logging.getLogger(__name__)


@dataclass
class ProbedAccount:
    """Result of probing a CDP port for an IG login."""

    port: int
    user_id: str | None = None
    username: str | None = None
    full_name: str | None = None
    profile_pic_url: str | None = None
    is_private: bool = False
    is_verified: bool = False
    follower_count: int = 0
    error: str | None = None


def probe_port(port: int, timeout: int = 15) -> ProbedAccount:
    """Probe a CDP port for the logged-in IG account.

    Returns a ProbedAccount with user_id, username, etc.
    On failure, error is set and user_id/username are None.
    """
    result = ProbedAccount(port=port)
    base_url = f"http://localhost:{port}"

    # 1. Find IG tab
    tab = TabDiscovery.find_ig_tab(base_url)
    if not tab:
        # Try any real tab and navigate to IG
        tabs = TabDiscovery.list_tabs(base_url)
        real_tabs = [
            t for t in tabs
            if t.get("url", "").startswith("http")
            and "chrome-extension" not in t.get("url", "")
            and "blob:" not in t.get("url", "")
        ]
        if not real_tabs:
            result.error = "No tabs found on port"
            return result
        tab = real_tabs[0]
        cdp = CDPClient()
        cdp.connect(tab["webSocketDebuggerUrl"])
        try:
            cdp.navigate("https://www.instagram.com/", wait=5)
        except Exception as e:
            result.error = f"Failed to navigate to IG: {e}"
            cdp.close()
            return result
    else:
        cdp = CDPClient()
        cdp.connect(tab["webSocketDebuggerUrl"])

    try:
        # 2. Read ds_user_id + csrftoken from cookies
        cookie_js = """(() => {
            let uid = '';
            let csrf = '';
            let m = document.cookie.match(/ds_user_id=([^;]+)/);
            if (m) uid = m[1];
            m = document.cookie.match(/csrftoken=([^;]+)/);
            if (m) csrf = m[1];
            return JSON.stringify({user_id: uid, csrftoken: csrf});
        })()"""

        resp = cdp.evaluate(cookie_js)
        if not resp or resp == "E":
            result.error = "No IG cookies found (not logged in)"
            return result

        try:
            cookies = json.loads(resp)
        except (json.JSONDecodeError, TypeError):
            result.error = f"Failed to parse cookies: {resp}"
            return result

        user_id = cookies.get("user_id", "")
        csrf = cookies.get("csrftoken", "")

        if not user_id:
            result.error = "ds_user_id cookie missing — not logged in"
            return result

        result.user_id = user_id

        # 3. Resolve user_id → username via IG internal API
        resolve_js = f"""(async () => {{
            let r = await fetch('https://i.instagram.com/api/v1/users/{user_id}/info/', {{
                headers: {{
                    'x-csrftoken': '{csrf}',
                    'x-ig-app-id': '936619743392459'
                }}
            }});
            let d = await r.json();
            let u = d.user || {{}};
            return JSON.stringify({{
                username: u.username || '',
                full_name: u.full_name || '',
                profile_pic_url: u.profile_pic_url || '',
                is_private: u.is_private || false,
                is_verified: u.is_verified || false,
                follower_count: u.follower_count || 0
            }});
        }})()"""

        resolve_resp = cdp.evaluate(resolve_js)
        if not resolve_resp or resolve_resp == "E":
            # Fallback: at least we have user_id, try to get username from page
            result.error = f"API resolve failed for uid={user_id}"
            result.username = None
            return result

        try:
            user_data = json.loads(resolve_resp)
            result.username = user_data.get("username") or None
            result.full_name = user_data.get("full_name") or None
            result.profile_pic_url = user_data.get("profile_pic_url") or None
            result.is_private = bool(user_data.get("is_private"))
            result.is_verified = bool(user_data.get("is_verified"))
            result.follower_count = int(user_data.get("follower_count") or 0)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            result.error = f"Failed to parse user API response: {e}"
            return result

        return result

    finally:
        cdp.close()


def probe_all_ports(ports: list[int]) -> list[ProbedAccount]:
    """Probe multiple CDP ports and return results.

    Skips ports that aren't reachable (logs warning, returns error entry).
    """
    results = []
    for port in ports:
        try:
            result = probe_port(port)
            if result.username:
                logger.info(
                    "Port %d: @%s (uid=%s)", port, result.username, result.user_id
                )
            elif result.error:
                logger.warning("Port %d: %s", port, result.error)
            results.append(result)
        except Exception as e:
            logger.error("Port %d: probe failed — %s", port, e)
            results.append(ProbedAccount(port=port, error=str(e)))

    return results
