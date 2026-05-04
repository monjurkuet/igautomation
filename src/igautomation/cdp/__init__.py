"""CDP (Chrome DevTools Protocol) client and tab discovery."""

from .client import CDPClient
from .discovery import TabDiscovery

__all__ = ["CDPClient", "TabDiscovery"]
