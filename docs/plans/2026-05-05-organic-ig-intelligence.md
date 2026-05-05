# Organic IG Intelligence Platform — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build an LLM-driven, daemonized Instagram intelligence platform that discovers, profiles, and monitors Bangladeshi female influencers at every tier — while mimicking organic human behavior to avoid detection.

**Architecture:** The current igautomation codebase becomes the foundation. We restructure it into 4 layers: (1) a behavior engine that simulates human browsing patterns via CDP, (2) an intelligence layer that discovers/profiles/accounts, (3) a proper SQLite schema for rich data storage, and (4) an LLM-driven orchestrator daemon that Hermes can supervise — analyzing data quality, adjusting strategy, and prioritizing targets.

**Tech Stack:** Python 3.11, Chrome DevTools Protocol, SQLite (with proper schema), Typer CLI, asyncio, Hermes cron/daemon integration

---

## Phase 0: Project Restructure

### Task 1: Set up uv venv and restructure package

**Objective:** Move from global Python install to proper uv-managed venv, restructure src layout.

**Files:**
- Modify: `pyproject.toml`
- Create: `.python-version`

**Step 1: Initialize uv project**

```bash
cd ~/projects/igautomation
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[dev]"
```

**Step 2: Add new dependencies to pyproject.toml**

Add to `[project.dependencies]`:
```
"aiosqlite>=0.19",
"pydantic>=2.0",
"croniter>=1.4",
```

**Step 3: Verify install**

Run: `igx tabs`
Expected: lists Chrome tabs (same as before, but from venv)

**Step 4: Commit**

```bash
git add -A && git commit -m "chore: set up uv venv and add new deps"
```

---

## Phase 1: Human Behavior Engine

This is the most critical layer. Every action the system takes must look like a real person browsing. No rapid-fire API calls. No predictable patterns.

### Task 2: Create behavior configuration model

**Objective:** Define all tunable behavior parameters as a Pydantic model — delays, session lengths, action ratios, rate limits.

**Files:**
- Create: `src/igautomation/behavior/__init__.py`
- Create: `src/igautomation/behavior/config.py`
- Create: `tests/test_behavior_config.py`

**Step 1: Write failing test**

```python
# tests/test_behavior_config.py
from igautomation.behavior.config import BehaviorConfig, SessionConfig

def test_behavior_config_defaults():
    cfg = BehaviorConfig()
    assert cfg.action_delay_min == 2.0
    assert cfg.action_delay_max == 8.0
    assert cfg.session_duration_min == 300  # 5 min
    assert cfg.session_duration_max == 1800  # 30 min
    assert cfg.likes_per_session_max == 20
    assert cfg.follows_per_session_max == 5

def test_session_config():
    cfg = BehaviorConfig()
    session = cfg.new_session()
    assert session.duration_seconds >= 300
    assert session.duration_seconds <= 1800
    assert session.max_likes <= 20
    assert session.max_follows <= 5
```

**Step 2: Run test to verify failure**

Run: `pytest tests/test_behavior_config.py -v`
Expected: FAIL — module not found

**Step 3: Write implementation**

```python
# src/igautomation/behavior/__init__.py
from .config import BehaviorConfig, SessionConfig

# src/igautomation/behavior/config.py
from __future__ import annotations
import random
from dataclasses import dataclass, field
from pydantic import BaseModel

class BehaviorConfig(BaseModel):
    """Tunable parameters for organic behavior simulation."""
    # Delays between actions (seconds)
    action_delay_min: float = 2.0
    action_delay_max: float = 8.0
    # Scroll behavior
    scroll_delay_min: float = 1.5
    scroll_delay_max: float = 5.0
    scroll_jitter: float = 0.3  # random variance in scroll distance
    # Session limits
    session_duration_min: int = 300   # 5 minutes
    session_duration_max: int = 1800  # 30 minutes
    # Action caps per session
    likes_per_session_max: int = 20
    follows_per_session_max: int = 5
    profile_views_per_session_max: int = 30
    reel_views_per_session_max: int = 10
    searches_per_session_max: int = 8
    # Cooldown between sessions
    session_cooldown_min: int = 600   # 10 minutes
    session_cooldown_max: int = 3600  # 60 minutes
    # Daily caps
    daily_likes_max: int = 80
    daily_follows_max: int = 20
    daily_profile_views_max: int = 100
    # Read-dwell: time spent "reading" a post before liking (seconds)
    read_dwell_min: float = 3.0
    read_dwell_max: float = 12.0

    def new_session(self) -> SessionConfig:
        return SessionConfig(
            duration_seconds=random.randint(self.session_duration_min, self.session_duration_max),
            max_likes=random.randint(1, self.likes_per_session_max),
            max_follows=random.randint(1, self.follows_per_session_max),
            max_profile_views=random.randint(5, self.profile_views_per_session_max),
            max_reel_views=random.randint(2, self.reel_views_per_session_max),
            max_searches=random.randint(2, self.searches_per_session_max),
        )

    def action_delay(self) -> float:
        return random.uniform(self.action_delay_min, self.action_delay_max)

    def scroll_delay(self) -> float:
        return random.uniform(self.scroll_delay_min, self.scroll_delay_max)

    def read_dwell(self) -> float:
        return random.uniform(self.read_dwell_min, self.read_dwell_max)

    def cooldown_seconds(self) -> int:
        return random.randint(self.session_cooldown_min, self.session_cooldown_max)


@dataclass
class SessionConfig:
    """Randomized budget for a single browsing session."""
    duration_seconds: int
    max_likes: int
    max_follows: int
    max_profile_views: int
    max_reel_views: int
    max_searches: int
    likes_used: int = 0
    follows_used: int = 0
    profile_views_used: int = 0
    reel_views_used: int = 0
    searches_used: int = 0
    started_at: float = 0.0

    def can_like(self) -> bool:
        return self.likes_used < self.max_likes

    def can_follow(self) -> bool:
        return self.follows_used < self.max_follows

    def can_view_profile(self) -> bool:
        return self.profile_views_used < self.max_profile_views

    def can_view_reel(self) -> bool:
        return self.reel_views_used < self.max_reel_views

    def can_search(self) -> bool:
        return self.searches_used < self.max_searches

    def time_remaining(self) -> float:
        import time
        if not self.started_at:
            return self.duration_seconds
        return max(0, self.duration_seconds - (time.time() - self.started_at))

    def is_exhausted(self) -> bool:
        return self.time_remaining() <= 0
```

**Step 4: Run test to verify pass**

Run: `pytest tests/test_behavior_config.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/igautomation/behavior/ tests/test_behavior_config.py
git commit -m "feat: add behavior configuration model for organic simulation"
```

---

### Task 3: Create the BehaviorEngine — action executor with human-like timing

**Objective:** A class that wraps CDPClient actions with randomized delays, session budgets, and human-like scroll/interaction patterns. All actual IG interactions go through this engine.

**Files:**
- Create: `src/igautomation/behavior/engine.py`
- Create: `tests/test_behavior_engine.py`

**Step 1: Write failing test**

```python
# tests/test_behavior_engine.py
import time
from unittest.mock import MagicMock, patch
from igautomation.behavior.config import BehaviorConfig, SessionConfig
from igautomation.behavior.engine import BehaviorEngine

def test_engine_respects_session_budget():
    cdp = MagicMock()
    cfg = BehaviorConfig(likes_per_session_max=2, follows_per_session_max=1)
    session = SessionConfig(duration_seconds=60, max_likes=2, max_follows=1,
                            max_profile_views=10, max_reel_views=5, max_searches=3)
    engine = BehaviorEngine(cdp, cfg, session)

    # Should allow up to 2 likes
    assert engine.can_like()
    session.likes_used = 2
    assert not engine.can_like()

    # Should allow up to 1 follow
    assert engine.can_follow()
    session.follows_used = 1
    assert not engine.can_follow()

def test_engine_scroll_returns_profile_links():
    cdp = MagicMock()
    cdp.scroll.return_value = ["user1", "user2", "user3"]
    cfg = BehaviorConfig()
    session = cfg.new_session()
    engine = BehaviorEngine(cdp, cfg, session)

    with patch("time.sleep"):  # skip delays in test
        links = engine.scroll_feed(max_scrolls=2)
    assert len(links) == 3
```

**Step 2: Run test to verify failure**

Run: `pytest tests/test_behavior_engine.py -v`
Expected: FAIL — module not found

**Step 3: Write implementation**

```python
# src/igautomation/behavior/engine.py
from __future__ import annotations
import logging
import random
import time
from igautomation.cdp.client import CDPClient
from igautomation.behavior.config import BehaviorConfig, SessionConfig

logger = logging.getLogger(__name__)


class BehaviorEngine:
    """Wraps CDP actions with human-like timing and session budget enforcement.

    Every IG interaction goes through this engine. It:
    - Adds randomized delays between actions
    - Enforces per-session and daily action caps
    - Simulates reading/dwelling on content before interacting
    - Logs every action for audit trail
    """

    def __init__(self, cdp: CDPClient, config: BehaviorConfig, session: SessionConfig):
        self.cdp = cdp
        self.config = config
        self.session = session
        self._daily_likes = 0
        self._daily_follows = 0
        self._daily_profile_views = 0

    def _delay(self):
        """Human-like pause between actions."""
        t = self.config.action_delay()
        logger.debug("action delay %.1fs", t)
        time.sleep(t)

    def _dwell(self):
        """Simulate reading/viewing content before interacting."""
        t = self.config.read_dwell()
        logger.debug("reading dwell %.1fs", t)
        time.sleep(t)

    # --- Budget checks ---

    def can_like(self) -> bool:
        return self.session.can_like() and self._daily_likes < self.config.daily_likes_max

    def can_follow(self) -> bool:
        return self.session.can_follow() and self._daily_follows < self.config.daily_follows_max

    def can_view_profile(self) -> bool:
        return self.session.can_view_profile() and self._daily_profile_views < self.config.daily_profile_views_max

    # --- Organic actions ---

    def scroll_feed(self, max_scrolls: int = 5) -> list[str]:
        """Scroll the feed like a human, collecting profile links."""
        self._delay()
        links = self.cdp.scroll(max_scrolls=max_scrolls, delay=self.config.scroll_delay())
        logger.info("scrolled feed %d times, found %d links", max_scrolls, len(links))
        return links

    def view_profile(self, username: str) -> dict | None:
        """Navigate to a profile, dwell like reading it."""
        if not self.can_view_profile():
            logger.info("profile view budget exhausted, skipping %s", username)
            return None
        self._delay()
        self.cdp.navigate(f"https://www.instagram.com/{username}/")
        self._dwell()
        self.session.profile_views_used += 1
        self._daily_profile_views += 1
        logger.info("viewed profile %s (%d/%d session, %d/%d daily)",
                     username, self.session.profile_views_used, self.session.max_profile_views,
                     self._daily_profile_views, self.config.daily_profile_views_max)
        return {"username": username, "viewed_at": time.time()}

    def like_post(self, post_url: str) -> bool:
        """Like a post — dwell first (simulating reading), then like."""
        if not self.can_like():
            logger.info("like budget exhausted, skipping %s", post_url)
            return False
        self.cdp.navigate(post_url)
        self._dwell()  # read the post first
        # Click the like button via JS
        self.cdp.evaluate(
            "document.querySelector('span[class*=\"like\"]')?.click() || "
            "document.querySelector('svg[aria-label=\"Like\"]')?.closest('div[role=\"button\"]')?.click()"
        )
        self.session.likes_used += 1
        self._daily_likes += 1
        self._delay()
        logger.info("liked post %s (%d/%d session)", post_url, self.session.likes_used, self.session.max_likes)
        return True

    def follow_user(self, username: str) -> bool:
        """Follow a user — navigate to profile, click follow."""
        if not self.can_follow():
            logger.info("follow budget exhausted, skipping %s", username)
            return False
        self._delay()
        self.cdp.navigate(f"https://www.instagram.com/{username}/")
        self._dwell()
        # Click follow button
        result = self.cdp.evaluate(
            "document.querySelector('button:has(> div > span)')?.click(); "
            "Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Follow'))?.click() ? 'followed' : 'not_found'"
        )
        self.session.follows_used += 1
        self._daily_follows += 1
        logger.info("followed %s — result: %s", username, result)
        return True

    def search_and_browse(self, query: str, graphql) -> list[dict]:
        """Search for users, browse results organically."""
        if not self.session.can_search():
            logger.info("search budget exhausted")
            return []
        self._delay()
        results = graphql.search_users(query)
        self.session.searches_used += 1
        logger.info("searched '%s', found %d results", query, len(results))
        return results

    def watch_reel(self, reel_url: str) -> bool:
        """Watch a reel — navigate, wait for realistic watch time."""
        if not self.session.can_view_reel():
            return False
        self._delay()
        self.cdp.navigate(reel_url)
        watch_time = random.uniform(3.0, 15.0)  # partial or full watch
        time.sleep(watch_time)
        self.session.reel_views_used += 1
        logger.info("watched reel %s for %.1fs", reel_url, watch_time)
        return True

    def run_session_loop(self, actions: list[callable]):
        """Run a sequence of action callables until session budget/time is exhausted."""
        self.session.started_at = time.time()
        logger.info("session started — budget: %d likes, %d follows, %d profiles, %d reels, %d searches, %ds",
                     self.session.max_likes, self.session.max_follows,
                     self.session.max_profile_views, self.session.max_reel_views,
                     self.session.max_searches, self.session.duration_seconds)

        for action_fn in actions:
            if self.session.is_exhausted():
                logger.info("session time exhausted")
                break
            try:
                action_fn(self)
            except Exception as e:
                logger.error("action failed: %s", e, exc_info=True)
                # On error, take a longer break (looks human + avoids cascading failures)
                time.sleep(random.uniform(10, 30))
```

**Step 4: Run test to verify pass**

Run: `pytest tests/test_behavior_engine.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/igautomation/behavior/engine.py tests/test_behavior_engine.py
git commit -m "feat: add BehaviorEngine with human-like action timing and session budgets"
```

---

## Phase 2: Proper Database Schema

The current SQLiteStore is too flat. We need a schema that captures the full lifecycle of each account — discovery, profiling, monitoring, interaction history.

### Task 4: Design and implement the database schema

**Objective:** Replace the flat `accounts` table with a proper relational schema using aiosqlite.

**Files:**
- Create: `src/igautomation/storage/schema.py`
- Create: `tests/test_storage_schema.py`

**Schema design:**

```sql
-- Core account data
CREATE TABLE accounts (
    username TEXT PRIMARY KEY,
    pk TEXT,                          -- IG numeric user ID
    full_name TEXT DEFAULT '',
    bio TEXT DEFAULT '',
    profile_pic_url TEXT DEFAULT '',
    is_verified INTEGER DEFAULT 0,
    is_private INTEGER DEFAULT 0,
    follower_count INTEGER,
    following_count INTEGER,
    post_count INTEGER,
    -- Classification
    is_bd INTEGER DEFAULT 0,         -- Bangladeshi
    is_female INTEGER DEFAULT 0,     -- Detected female
    influencer_tier TEXT,            -- 'mega' | 'macro' | 'micro' | 'nano' | 'upcoming'
    category TEXT,                   -- model, actress, lifestyle, fashion, beauty, fitness, etc
    -- Metadata
    first_seen_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_checked_at TEXT,
    last_interacted_at TEXT,
    is_following INTEGER DEFAULT 0,  -- Are we following them?
    is_followed_by INTEGER DEFAULT 0, -- Are they following us?
    data_quality_score REAL,         -- 0-1, LLM-assessed completeness
    notes TEXT DEFAULT ''            -- LLM observations
);

-- How we found each account
CREATE TABLE discovery_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    strategy TEXT NOT NULL,          -- 'suggestions' | 'search' | 'hashtag' | 'shoutout' | 'cascade' | 'feed_scroll' | 'reel' | 'suggested_follows'
    source_username TEXT,            -- Who led us here (null for search/hashtag)
    source_detail TEXT,              -- The search query, hashtag, etc.
    discovered_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (username) REFERENCES accounts(username)
);

-- Every interaction we have with an account
CREATE TABLE interaction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    action TEXT NOT NULL,            -- 'view_profile' | 'like' | 'follow' | 'unfollow' | 'watch_reel' | 'comment_view' | 'dm'
    action_detail TEXT,              -- Post URL, reel URL, etc.
    session_id TEXT,                 -- Tie to a specific daemon session
    performed_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (username) REFERENCES accounts(username)
);

-- Periodic snapshots of follower counts
CREATE TABLE follower_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    follower_count INTEGER,
    following_count INTEGER,
    post_count INTEGER,
    snapped_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (username) REFERENCES accounts(username)
);

-- Daemon session tracking
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,             -- UUID
    started_at TEXT,
    ended_at TEXT,
    actions_total INTEGER DEFAULT 0,
    accounts_discovered INTEGER DEFAULT 0,
    accounts_interacted INTEGER DEFAULT 0,
    strategy_snapshot TEXT           -- JSON of the strategy config used
);

-- LLM analysis results
CREATE TABLE analysis_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_type TEXT NOT NULL,     -- 'data_quality' | 'strategy_review' | 'account_scoring' | 'tier_assignment'
    input_summary TEXT,              -- What data was analyzed
    result TEXT,                     -- LLM output
    model_used TEXT,
    performed_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Indexes
CREATE INDEX idx_accounts_tier ON accounts(influencer_tier);
CREATE INDEX idx_accounts_bd_female ON accounts(is_bd, is_female);
CREATE INDEX idx_accounts_following ON accounts(is_following);
CREATE INDEX idx_discovery_username ON discovery_events(username);
CREATE INDEX idx_discovery_strategy ON discovery_events(strategy);
CREATE INDEX idx_interaction_username ON interaction_log(username);
CREATE INDEX idx_interaction_action ON interaction_log(action);
CREATE INDEX idx_snapshots_username ON follower_snapshots(username);
CREATE INDEX idx_snapshots_time ON follower_snapshots(snapped_at);
```

**Step 1: Write failing test**

```python
# tests/test_storage_schema.py
import tempfile
import os
from igautomation.storage.schema import Database

async def test_database_initialization():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        await db.initialize()
        # Verify all tables exist
        tables = await db.list_tables()
        assert "accounts" in tables
        assert "discovery_events" in tables
        assert "interaction_log" in tables
        assert "follower_snapshots" in tables
        assert "sessions" in tables
        assert "analysis_log" in tables

async def test_upsert_account():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        await db.initialize()
        await db.upsert_account(username="testuser", pk="12345", full_name="Test User", is_bd=1, is_female=1)
        account = await db.get_account("testuser")
        assert account["username"] == "testuser"
        assert account["pk"] == "12345"
        assert account["is_bd"] == 1
        assert account["last_checked_at"] is not None

async def test_discovery_event():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        await db.initialize()
        await db.upsert_account(username="testuser", pk="12345")
        await db.record_discovery(username="testuser", strategy="suggestions", source_username="seed_user")
        events = await db.get_discovery_events("testuser")
        assert len(events) == 1
        assert events[0]["strategy"] == "suggestions"
```

**Step 2: Run test to verify failure**

Run: `pytest tests/test_storage_schema.py -v`
Expected: FAIL — module not found

**Step 3: Write implementation**

The `Database` class in `schema.py` will use aiosqlite and provide:
- `initialize()` — creates all tables + indexes
- `upsert_account(**fields)` — INSERT OR REPLACE with `last_checked_at` auto-set
- `record_discovery(username, strategy, source_username, source_detail)`
- `record_interaction(username, action, action_detail, session_id)`
- `record_follower_snapshot(username, follower_count, following_count, post_count)`
- `get_account(username)` → dict | None
- `get_discovery_events(username)` → list[dict]
- `get_interaction_log(username, limit=50)` → list[dict]
- `get_accounts_by_tier(tier)` → list[dict]
- `get_bd_female_influencers()` → list[dict]
- `list_tables()` → list[str]
- `count_accounts()` → int

(Implementation will be ~200 lines with aiosqlite — straightforward mapping of the schema above.)

**Step 4: Run test to verify pass**

Run: `pytest tests/test_storage_schema.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/igautomation/storage/schema.py tests/test_storage_schema.py
git commit -m "feat: add proper relational database schema for IG intelligence"
```

---

## Phase 3: Intelligence Layer — Discovery & Profiling

### Task 5: Refactor ProfileAnalyzer to use the new database and GraphQL (no navigation)

**Objective:** Replace page-navigation-based profiling with GraphQL-only enrichment. Populate all account fields including bio.

**Files:**
- Modify: `src/igautomation/scraper/analyzer.py`
- Create: `tests/test_analyzer.py`

**Key changes:**
- Remove `navigate()` calls — use `web_profile_info` GraphQL API instead (already working in `get_user_id`)
- Extract bio from GraphQL response
- Detect female from name/bio heuristics (Bangladeshi names have patterns)
- Assign influencer_tier based on follower count:
  - mega: 500K+
  - macro: 100K-500K
  - micro: 10K-100K
  - nano: 1K-10K
  - upcoming: <1K
- Detect category from bio keywords (model, actress, lifestyle, etc.)
- Write results to the new Database instead of flat files

---

### Task 6: Expand discovery strategies with organic actions

**Objective:** Add new discovery strategies that leverage organic browsing behavior.

**Files:**
- Modify: `src/igautomation/scraper/collector.py`

**New strategies to add:**
7. **Feed scroll discovery** — scroll your home feed, collect profiles from posts
8. **Reel browse discovery** — watch reels, collect profiles from reel authors
9. **Suggested follows discovery** — use IG's "Suggested for You" feature
10. **Explore page discovery** — browse the Explore tab for BD content

Each strategy goes through the BehaviorEngine (respects budgets, adds delays).

---

## Phase 4: LLM-Driven Orchestrator Daemon

### Task 7: Create the daemon loop

**Objective:** A long-running process that runs sessions with cooldowns between them, orchestrated by an LLM planner.

**Files:**
- Create: `src/igautomation/daemon/__init__.py`
- Create: `src/igautomation/daemon/loop.py`
- Create: `src/igautomation/daemon/strategies.py`

**Daemon behavior:**

```
┌─────────────────────────────────────┐
│           Daemon Loop               │
│                                     │
│  1. LLM picks strategy for session  │
│  2. BehaviorEngine runs session     │
│  3. Session data saved to DB        │
│  4. LLM reviews data quality       │
│  5. LLM adjusts strategy            │
│  6. Cooldown (10-60 min)            │
│  7. Go to 1                         │
└─────────────────────────────────────┘
```

**Strategy selection by LLM:**

The daemon calls the configured LLM (via Hermes's API) before each session with a prompt like:

```
You are an Instagram intelligence analyst. Current stats:
- Total accounts in DB: 1,247
- BD female influencers: 389
- By tier: mega=3, macro=15, micro=89, nano=142, upcoming=140
- Sessions today: 4
- Discovery success rates: suggestions=72%, search=45%, cascade=88%, feed_scroll=23%
- Accounts needing profile refresh: 234
- Recent follow-back rate: 12%

Pick the next session's primary strategy and parameters. Options:
- discovery (which strategy, what seeds/queries)
- profiling (batch of accounts needing enrichment)
- monitoring (re-check follower counts for tracked accounts)
- engagement (like/follow to maintain organic appearance)

Respond in JSON: {"strategy": "...", "params": {...}, "rationale": "..."}
```

### Task 8: Create the CLI daemon commands

**Objective:** Add `igx daemon start/stop/status` commands.

**Files:**
- Modify: `src/igautomation/cli.py`

**Commands:**
```
igx daemon start --config daemon.yaml   # Start daemon (foreground or background)
igx daemon stop                         # Stop daemon
igx daemon status                       # Show current state, session history
igx daemon analyze                      # Trigger LLM analysis on current data
```

---

## Phase 5: LLM Analysis & Data Quality

### Task 9: Create analysis module

**Objective:** LLM-powered analysis of collected data — quality scoring, strategy adjustment, tier verification.

**Files:**
- Create: `src/igautomation/analysis/__init__.py`
- Create: `src/igautomation/analysis/quality.py`
- Create: `src/igautomation/analysis/strategy.py`

**Analysis types:**

1. **Data quality audit** — scan accounts with missing fields, stale data, conflicting signals. Score each account 0-1 on completeness. Flag accounts needing re-check.

2. **Strategy effectiveness review** — compare discovery strategies by yield (new quality accounts per hour). Recommend which strategies to weight more/less.

3. **Tier verification** — LLM reviews borderline accounts (e.g. someone with 9.5K followers — is she nano or micro? Check engagement rate, growth trajectory from snapshots).

4. **Category classification** — LLM reads bios and classifies accounts into categories: model, actress, lifestyle, fashion, beauty, fitness, food, travel, tech, music, comedy, other.

5. **False positive review** — Flag accounts that matched BD/female keywords but may not actually be (e.g. a brand account, a male photographer posting BD models, a shoutout page).

All analysis results go to `analysis_log` table. Hermes cron can trigger periodic reviews.

---

### Task 10: Hermes cron integration

**Objective:** Set up Hermes cron jobs for periodic data review and strategy adjustment.

**Files:**
- Create: `src/igautomation/daemon/cron_config.py`

**Cron schedule:**
- Every 6 hours: `igx daemon analyze --type quality` — data quality audit
- Every 12 hours: `igx daemon analyze --type strategy` — strategy effectiveness review
- Daily: `igx daemon analyze --type tier` — tier verification for borderline accounts
- Weekly: full database export to JSON backup

---

## Phase 6: Anti-Detection Hardening

### Task 11: Implement rate-limiting with exponential backoff

**Objective:** Add proper 429/response-code handling, adaptive rate limiting, and detection avoidance.

**Files:**
- Create: `src/igautomation/behavior/ratelimit.py`

**Features:**
- Track all API response codes in-session
- On 429 or "challenge" response: pause for exponential backoff (30s → 60s → 120s → 300s → session abort)
- On "checkpoint" or "login required": immediately abort session, notify via Hermes
- Adaptive rate: if requests succeed consistently, slowly reduce delay floor. On any warning signal, increase delays.
- Per-hour action tracking — never exceed IG's undocumented limits (estimated: ~200 likes/hour, ~60 follows/hour, ~300 API calls/hour)

### Task 12: Session pattern randomization

**Objective:** Make session timing unpredictable to avoid detection patterns.

**Files:**
- Modify: `src/igautomation/daemon/loop.py`

**Features:**
- Sessions don't start at fixed intervals — use jitter around the cooldown
- Occasional "skip sessions" — randomly take a longer break (1-3 hours) to simulate real-life interruptions
- Session duration varies significantly (5 min to 30 min)
- Different session types have different timing profiles (discovery sessions are shorter, engagement sessions are longer)
- Never run sessions during "sleep hours" (2am-7am local time) — real users sleep

---

## Phase 7: Migration & CLI Polish

### Task 13: Migrate existing flat data to new schema

**Objective:** One-time migration script to move data from old JSON/CSV/SQLite to the new schema.

**Files:**
- Create: `src/igautomation/migration.py`
- Add: `igx migrate` CLI command

**Step:** Read `output/accounts.json` + `output/igautomation.db`, parse into new schema format, insert via `Database.upsert_account()`.

### Task 14: Update CLI with new commands and clean up dead code

**Objective:** Full CLI refresh with all new commands, remove dead code.

**Files:**
- Modify: `src/igautomation/cli.py`
- Modify: `src/igautomation/graphql/client.py` — remove `DOC_PROFILE_CONTENT`

**New CLI surface:**
```
igx tabs                           # List Chrome tabs (existing)
igx discover [seeds]               # Discover accounts (existing, enhanced)
igx search <query>                 # Search users (existing)
igx suggest <username>             # Get suggestions (existing)
igx analyze                        # Analyze profiles (existing, enhanced)
igx session                        # Run a single organic session
igx daemon start                   # Start daemon
igx daemon stop                    # Stop daemon
igx daemon status                  # Daemon status
igx daemon analyze [--type TYPE]   # Run LLM analysis
igx db stats                       # Database statistics
igx db export                      # Export to JSON/CSV
igx migrate                        # Migrate old data to new schema
```

---

## Implementation Order Summary

| Phase | Tasks | What it delivers |
|-------|-------|-----------------|
| 0 | 1 | uv venv, proper deps |
| 1 | 2-3 | BehaviorConfig + BehaviorEngine (organic action layer) |
| 2 | 4 | Proper relational database schema |
| 3 | 5-6 | GraphQL profiling + new discovery strategies |
| 4 | 7-8 | LLM-driven daemon loop + CLI commands |
| 5 | 9-10 | LLM analysis module + Hermes cron |
| 6 | 11-12 | Anti-detection hardening |
| 7 | 13-14 | Migration + CLI polish |

**Total: 14 tasks across 7 phases.**

Each phase builds on the previous one. Phases 1-2 are the foundation — once the behavior engine and database schema are in place, everything else plugs in cleanly.

The daemon (Phase 4) is where it gets powerful — the LLM decides *what to do next* based on current data state, making the system adaptive rather than scripted.

---

## Key Anti-Detection Principles

1. **Be slow.** Real users don't make 100 API calls in 10 minutes. Our current cascade that collected 827 accounts in <10 min is exactly the pattern that gets flagged.

2. **Be unpredictable.** Randomize everything — delays, session lengths, action order, cooldowns. No two sessions should look the same.

3. **Be incomplete.** Real users don't scroll through an entire suggestion list. They browse a bit, get distracted, come back later. The behavior engine should sometimes abandon actions mid-way.

4. **Diversify actions.** A session that only searches looks like a bot. Mix searches, profile views, feed scrolling, reel watching, and occasional likes/follows.

5. **Respect daily limits.** Instagram's limits are undocumented but well-known from community testing. Stay well below them.

6. **Sleep at night.** No sessions during 2am-7am. Real humans sleep.

7. **Back off on any warning.** If IG shows any challenge, checkpoint, or rate-limit signal — stop immediately and wait much longer before retrying.
