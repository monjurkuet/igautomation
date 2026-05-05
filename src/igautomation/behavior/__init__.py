"""Behavior simulation layer — human-like timing, session budgets, and rate limiting."""

from igautomation.behavior.config import BehaviorConfig, SessionConfig
from igautomation.behavior.engine import BehaviorEngine
from igautomation.behavior.rate_limiter import RateLimitConfig, RateLimiter

__all__ = [
    "BehaviorConfig",
    "BehaviorEngine",
    "RateLimitConfig",
    "RateLimiter",
    "SessionConfig",
]
