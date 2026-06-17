"""CDP (Chrome DevTools Protocol) client and tab discovery."""

from .client import CDPClient, SKIP_USERNAMES, _USERNAME_RE
from .discovery import TabDiscovery

__all__ = ["CDPClient", "TabDiscovery", "SKIP_USERNAMES", "_USERNAME_RE"]
