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
DOC_PROFILE_CONTENT = "25858451687162830"

IG_APP_ID = "936619743392459"


class GraphQLClient:
    """Execute Instagram GraphQL queries through the logged-in browser.

    All calls use ``CDPClient.evaluate()`` to run ``fetch()`` inside the
    browser page, which automatically includes cookies and CSRF tokens.
    """

    def __init__(self, cdp: CDPClient) -> None:
        self._cdp = cdp

    def _csrf_token(self) -> str:
        """Read the csrftoken cookie from the browser."""
        js = 'document.cookie.match(/csrftoken=([^;]+)/)?.[1] || ""'
        result = self._cdp.evaluate(js, timeout=5)
        return result or ""

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
            .then(function(r) {{ return r.json(); }})
            .then(function(data) {{ return JSON.stringify(data); }})
            .catch(function(e) {{ return JSON.stringify({{error: e.message}}); }});
        }})()
        """
        raw = self._cdp.evaluate(js, timeout=20)
        if not raw:
            logger.warning("GraphQL fetch returned no data (doc_id=%s)", doc_id)
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("GraphQL response not valid JSON (doc_id=%s)", doc_id)
            return None

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
        js = f"""
        (function() {{
            return fetch('/api/v1/web/search/topsearch/?query={encoded}&context=blended', {{
                method: 'GET',
                headers: {{
                    'X-IG-App-ID': '{IG_APP_ID}',
                    'X-CSRFToken': document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '',
                    'X-Requested-With': 'XMLHttpRequest',
                }}
            }})
            .then(function(r) {{ return r.json(); }})
            .then(function(data) {{ return JSON.stringify(data); }})
            .catch(function(e) {{ return JSON.stringify({{error: e.message}}); }});
        }})()
        """
        raw = self._cdp.evaluate(js, timeout=15)
        if not raw:
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
        # Method 1: web_profile_info API
        js = f"""
        (function() {{
            return fetch('/api/v1/users/web_profile_info/?username={username}', {{
                method: 'GET',
                headers: {{
                    'X-IG-App-ID': '{IG_APP_ID}',
                    'X-CSRFToken': document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '',
                    'X-Requested-With': 'XMLHttpRequest',
                }}
            }})
            .then(function(r) {{ return r.json(); }})
            .then(function(data) {{
                var userId = data?.data?.user?.id;
                if (userId) return userId;
                // Fallback: walk the response for any numeric id
                var str = JSON.stringify(data);
                var m = str.match(/"id"\\s*:\\s*"?(\\d{{5,}})/);
                return m ? m[1] : null;
            }})
            .catch(function() {{ return null; }});
        }})()
        """
        result = self._cdp.evaluate(js, timeout=15)
        if result and result not in ("None", "null", ""):
            logger.debug("Resolved @%s → %s via web_profile_info", username, result)
            return result

        # Method 2: navigate and parse page scripts
        logger.debug("web_profile_info failed for @%s, trying page scrape", username)
        self._cdp.navigate(f"https://www.instagram.com/{username}/", wait=4)
        js2 = """
        (function() {
            var scripts = document.querySelectorAll('script');
            for (var i = 0; i < scripts.length; i++) {
                var text = scripts[i].textContent || '';
                var match = text.match(/"user_id"\\s*:\\s*"?(\d{5,})"?/);
                if (match) return match[1];
                match = text.match(/"pk"\\s*:\\s*"?(\d{5,})"?/);
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

    def get_profile_meta(self, username: str) -> dict[str, str] | None:
        """Get profile metadata via og:description meta tag.

        Args:
            username: Instagram username.

        Returns:
            Dict with 'meta', 'title' keys, or None if profile not found.
        """
        self._cdp.navigate(f"https://www.instagram.com/{username}/", wait=3)
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
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if "not found" in (data.get("title", "") + data.get("meta", "")).lower():
                return None
            return data
        except json.JSONDecodeError:
            return None

    def get_discover_people(self) -> list[str]:
        """Fetch the Discover People / suggested users for the logged-in account.

        Returns:
            List of usernames.
        """
        js = f"""
        (function() {{
            return fetch('/api/v1/web/discover/people/', {{
                method: 'GET',
                headers: {{
                    'X-IG-App-ID': '{IG_APP_ID}',
                    'X-CSRFToken': document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '',
                    'X-Requested-With': 'XMLHttpRequest',
                }}
            }})
            .then(function(r) {{ return r.json(); }})
            .then(function(data) {{ return JSON.stringify(data); }})
            .catch(function(e) {{ return JSON.stringify({{error: e.message}}); }});
        }})()
        """
        raw = self._cdp.evaluate(js, timeout=15)
        if not raw:
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
