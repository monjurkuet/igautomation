"""Instagram GraphQL API wrapper.

Calls Instagram's internal GraphQL endpoints via fetch() inside the
logged-in browser session. This bypasses CORS and auth issues because
the browser already has all the right cookies and CSRF tokens.

The key queries:
- PolarisProfileSuggestedUsersWithPreloadableQuery (doc_id: 25814188068245954)
- PolarisProfileSuggestedUsersWithLazyQueryQuery (doc_id: 25878289415125440)
- PolarisProfilePageContentQuery (doc_id: 25858451687162830)
- Users search API: /api/v1/users/search/
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any

from igautomation.cdp.client import CDPClient

logger = logging.getLogger(__name__)

# Instagram GraphQL doc IDs (these change with IG deploys but are fairly stable).
DOC_SUGGESTED_PRELOAD = "25814188068245954"
DOC_SUGGESTED_LAZY = "25878289415125440"

IG_APP_ID = "936619743392459"

# Sentinel returned by JS fetch when rate-limited (HTTP 429).
_RATE_LIMITED = "__RATE_LIMITED__"


class GraphQLClient:
    """Execute Instagram GraphQL queries through the logged-in browser.

    All calls use ``CDPClient.evaluate()`` to run ``fetch()`` inside the
    browser page, which automatically includes cookies and CSRF tokens.

    Attributes:
        rate_limited: Set to True when a 429 response is detected.
            Callers should check this and back off before retrying.
    """

    def __init__(self, cdp: CDPClient) -> None:
        self._cdp = cdp
        self.rate_limited: bool = False

    def _csrf_token(self) -> str:
        """Read the csrftoken cookie from the browser."""
        js = 'document.cookie.match(/csrftoken=([^;]+)/)?.[1] || ""'
        result = self._cdp.evaluate(js, timeout=5)
        return result or ""

    # ------------------------------------------------------------------
    # Internal: GraphQL via POST
    # ------------------------------------------------------------------

    def _fetch_graphql(
        self,
        doc_id: str,
        friendly_name: str,
        variables: dict[str, Any],
        endpoint: str = "/graphql/query",
    ) -> dict[str, Any] | None:
        """Send a GraphQL query via browser fetch() and return parsed JSON."""
        variables_json = json.dumps(variables, separators=(",", ":"))
        # Escape for JS string literal
        variables_escaped = variables_json.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")

        js = f"""
        (function() {{
            return fetch('{endpoint}', {{
                method: 'POST',
                headers: {{
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'X-IG-App-ID': '{IG_APP_ID}',
                    'X-CSRFToken': document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '',
                    'X-Requested-With': 'XMLHttpRequest',
                }},
                body: new URLSearchParams({{
                    'fb_api_req_friendly_name': '{friendly_name}',
                    'doc_id': '{doc_id}',
                    'variables': '{variables_escaped}',
                }}).toString()
            }})
            .then(function(r) {{
                if (r.status === 429) return '{_RATE_LIMITED}';
                return r.json();
            }})
            .then(function(data) {{
                if (data === '{_RATE_LIMITED}') return '{_RATE_LIMITED}';
                return JSON.stringify(data);
            }})
            .catch(function(e) {{ return JSON.stringify({{error: e.message}}); }});
        }})()
        """
        raw = self._cdp.evaluate(js, timeout=20)
        if not raw:
            logger.warning("GraphQL fetch returned no data (doc_id=%s)", doc_id)
            return None
        if raw == _RATE_LIMITED:
            self.rate_limited = True
            logger.warning("GraphQL rate-limited (429) on doc_id=%s", doc_id)
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("GraphQL response not valid JSON (doc_id=%s)", doc_id)
            return None

    # ------------------------------------------------------------------
    # Internal: REST API via GET (with 429 handling)
    # ------------------------------------------------------------------

    def _fetch_rest(
        self,
        url: str,
        timeout: float = 15,
    ) -> str | None:
        """Send a GET request via browser fetch() with 429 detection.

        Returns:
            JSON string of the response body, or the _RATE_LIMITED sentinel
            if HTTP 429 was received, or None on error.
        """
        js = f"""
        (function() {{
            return fetch('{url}', {{
                method: 'GET',
                headers: {{
                    'X-IG-App-ID': '{IG_APP_ID}',
                    'X-CSRFToken': document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '',
                    'X-Requested-With': 'XMLHttpRequest',
                }}
            }})
            .then(function(r) {{
                if (r.status === 429) return '{_RATE_LIMITED}';
                return r.json();
            }})
            .then(function(data) {{
                if (data === '{_RATE_LIMITED}') return '{_RATE_LIMITED}';
                return JSON.stringify(data);
            }})
            .catch(function() {{ return null; }});
        }})()
        """
        raw = self._cdp.evaluate(js, timeout=timeout)
        if not raw or raw in ("None", "null", ""):
            return None
        if raw == _RATE_LIMITED:
            self.rate_limited = True
            logger.warning("REST API rate-limited (429) on %s", url[:80])
            return _RATE_LIMITED
        return raw

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_suggested_users(self, target_user_id: str) -> list[str]:
        """Fetch suggested/similar accounts for a given user ID.

        Args:
            target_user_id: Instagram numeric user ID.

        Returns:
            List of usernames extracted from the GraphQL response.
        """
        data = self._fetch_graphql(
            doc_id=DOC_SUGGESTED_PRELOAD,
            friendly_name="PolarisProfileSuggestedUsersWithPreloadableQuery",
            variables={"module": "profile", "target_id": target_user_id},
        )
        if not data:
            return []
        return _extract_usernames(data)

    def get_suggested_users_lazy(self, target_user_id: str) -> list[str]:
        """Fetch lazy-loaded suggested accounts for a given user ID.

        Args:
            target_user_id: Instagram numeric user ID.

        Returns:
            List of usernames.
        """
        data = self._fetch_graphql(
            doc_id=DOC_SUGGESTED_LAZY,
            friendly_name="PolarisProfileSuggestedUsersWithLazyQueryQuery",
            variables={"target_id": target_user_id, "module": "profile"},
        )
        if not data:
            return []
        return _extract_usernames(data)

    def search_users(self, query: str, count: int = 50) -> list[dict[str, Any]]:
        """Search Instagram users by query string.

        Uses the topsearch endpoint which returns richer results than the
        users/search endpoint.

        Args:
            query: Search text (e.g. "bangladeshi model").
            count: Max results to return (capped at API response size).

        Returns:
            List of user dicts with 'username', 'pk', 'full_name' keys.
        """
        encoded = urllib.parse.quote(query)
        raw = self._fetch_rest(f"/api/v1/web/search/topsearch/?query={encoded}&context=blended")
        if not raw or raw == _RATE_LIMITED:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []

        # topsearch returns {users: [{user: {username, pk, ...}}, ...]}
        items = data.get("users", [])
        results: list[dict[str, Any]] = []
        for item in items[:count]:
            user = item.get("user", item) if isinstance(item, dict) else item
            username = user.get("username", "")
            if not username:
                continue
            results.append({
                "username": username,
                "pk": str(user.get("pk", "")),
                "full_name": user.get("full_name", ""),
                "is_verified": user.get("is_verified", False),
                "profile_pic_url": user.get("profile_pic_url", ""),
            })
        return results

    def get_user_id(self, username: str) -> str | None:
        """Resolve a username to its numeric user ID.

        Uses the web_profile_info API endpoint which returns the user's
        numeric ID directly, falling back to page script parsing.

        Args:
            username: Instagram username (without @).

        Returns:
            Numeric user ID as string, or None.
        """
        if self.rate_limited:
            logger.warning("get_user_id skipped — rate limited")
            return None

        # Method 1: web_profile_info API
        raw = self._fetch_rest(f"/api/v1/users/web_profile_info/?username={username}")
        if raw == _RATE_LIMITED:
            return None
        if raw:
            try:
                data = json.loads(raw)
                user_id = data.get("data", {}).get("user", {}).get("id")
                if user_id:
                    logger.debug("Resolved @%s → %s via web_profile_info", username, user_id)
                    return str(user_id)
                # Fallback: walk the response for any numeric id
                data_str = json.dumps(data)
                import re
                m = re.search(r'"id"\s*:\s*"?(\\d{5,})"', data_str)
                if m:
                    logger.debug("Resolved @%s → %s via fallback scan", username, m.group(1))
                    return m.group(1)
            except json.JSONDecodeError:
                pass

        # Method 2: navigate and parse page scripts
        logger.debug("web_profile_info failed for @%s, trying page scrape", username)
        self._cdp.navigate(f"https://www.instagram.com/{username}/", wait=4)
        js2 = """
        (function() {
            var scripts = document.querySelectorAll('script');
            for (var i = 0; i < scripts.length; i++) {
                var text = scripts[i].textContent || '';
                var match = text.match(/"user_id"\\s*:\\s*"?(\\d{5,})"?/);
                if (match) return match[1];
                match = text.match(/"pk"\\s*:\\s*"?(\\d{5,})"?/);
                if (match) return match[1];
            }
            if (document.title.toLowerCase().includes('not found')) return 'NOT_FOUND';
            return null;
        })()
        """
        result2 = self._cdp.evaluate(js2, timeout=10)
        if result2 and result2 not in ("NOT_FOUND", "None", "null", ""):
            return result2

        return None

    def get_web_profile_info(self, username: str) -> dict[str, Any] | None:
        """Fetch full profile data for a user via the web_profile_info API.

        This is the GraphQL-only way to get bio, follower counts, full name,
        verification status, etc. — no page navigation needed.

        Handles HTTP 429 (rate limit) gracefully by setting the
        ``rate_limited`` flag and returning None.

        Args:
            username: Instagram username (without @).

        Returns:
            User data dict from the API, or None if not found or rate-limited.
        """
        if self.rate_limited:
            logger.warning("get_web_profile_info skipped — rate limited")
            return None

        raw = self._fetch_rest(f"/api/v1/users/web_profile_info/?username={username}")
        if not raw or raw == _RATE_LIMITED:
            return None
        try:
            data = json.loads(raw)
            user = data.get("data", {}).get("user")
            if user:
                return user
            logger.debug("web_profile_info: no user in response for @%s", username)
            return None
        except json.JSONDecodeError:
            logger.warning("web_profile_info: invalid JSON for @%s", username)
            return None

    def get_discover_people(self) -> list[str]:
        """Fetch the Discover People / suggested users for the logged-in account.

        Returns:
            List of usernames.
        """
        raw = self._fetch_rest("/api/v1/web/discover/people/")
        if not raw or raw == _RATE_LIMITED:
            return []
        try:
            data = json.loads(raw)
            return _extract_usernames(data)
        except json.JSONDecodeError:
            return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_usernames(data: Any, depth: int = 0) -> list[str]:
    """Recursively extract ``username`` fields from a GraphQL response."""
    if depth > 20:
        return []
    seen: set[str] = set()
    result: list[str] = []

    def _walk(obj: Any, d: int) -> None:
        if d > 20 or obj is None:
            return
        if isinstance(obj, dict):
            if "username" in obj and isinstance(obj["username"], str):
                name = obj["username"]
                if name and name not in seen:
                    seen.add(name)
                    result.append(name)
            for v in obj.values():
                _walk(v, d + 1)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item, d + 1)

    _walk(data, 0)
    return result
