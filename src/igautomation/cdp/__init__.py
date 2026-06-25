"""CDP (Chrome DevTools Protocol) client and tab discovery."""

from .client import CDPClient, SKIP_USERNAMES
from .discovery import TabDiscovery

__all__ = ["CDPClient", "TabDiscovery", "SKIP_USERNAMES"]
