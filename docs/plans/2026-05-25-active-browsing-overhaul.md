# Active Browsing Overhaul — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Transform igautomation from a mostly-idle scraper into an always-on IG power user that browses feeds/reels throughout the day, captures trending content organically, and engages with what it finds.

**Architecture:** Replace the current "session per strategy" model with a continuous browsing loop. The daemon now acts like a real user: scroll feed, swipe reels, explore trending — while passively harvesting every post/reel/account it sees. Engagement (like/follow/save) happens inline based on criteria, not as a separate strategy.

**Tech Stack:** Python 3.11, CDP via WebSocket, aiosqlite, existing BehaviorEngine/CDPClient

---

## Problem Statement

Current daemon ran 20 sessions over 20 days (3 active days). Only `discovery` strategy used. 512/532 accounts unclassified. 852/1168 content items never analyzed. Zero recurring follower snapshots. The daemon isn't behaving like a real user at all.

## Design Decisions

1. **Feed/reel scrolling = primary activity** — not a side effect of discovery. The daemon scrolls the main feed and Reels tab continuously, extracting every post/reel it sees.
2. **Inline engagement** — while scrolling, if content meets criteria (BD-relevant, trending, high engagement), like/save/follow immediately. No separate "engagement" session.
3. **Content harvesting from feed** — every post URL, username, and metadata visible in feed gets saved to `content_items` and `accounts` tables automatically.
4. **Reel swiping** — the Reels tab is infinite and algorithmically curated. Swiping through reels is the highest-value passive activity: it trains the algorithm AND captures trending content.
5. **Explore tab** — browse trending/Explore page for discovery beyond the feed.
6. **Higher daily session limits** — 12-16 sessions/day (real users check IG 8-20 times/day).
7. **Shorter session gaps** — 15-45 min between sessions (not 1-3 hours).
8. **Strategy priorities flipped** — `feed_browsing` and `reel_browsing` are now the top-priority strategies. Discovery/profiling are secondary.

---

### Task 1: Add `browse_feed` strategy to BehaviorEngine

**Objective:** New method that scrolls the main IG feed, extracts all visible post URLs + usernames, and saves them to DB. This is the core of "using IG like a real user."

**Files:**
- Modify: `src/igautomation/behavior/engine.py`
- Test: `tests/test_behavior_engine.py`

**Step 1: Add `browse_feed` method to BehaviorEngine**

Add after `scroll_feed` method (line ~111):

```python
def browse_feed(self, max_scrolls: int = 15) -> dict:
    """Scroll the main feed, extract post URLs, usernames, and metadata.

    Returns dict with keys: posts (list of dicts), usernames (list of str), scrolls_done (int)
    """
    self._delay()

    all_posts = []
    all_usernames = set()
    scrolls_done = 0

    for i in range(max_scrolls):
        if self._session.is_exhausted():
            break

        # Extract visible posts from current viewport
        extract_js = """(function() {
            var results = [];
            // Feed posts: articles or divs with role="main" descendants
            var links = document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]');
            links.forEach(function(a) {
                var href = a.getAttribute('href') || '';
                var username = '';
                // Username from href or parent context
                var parent = a.closest('article, div[role="button"]');
                if (parent) {
                    var userLinks = parent.querySelectorAll('a[href*="instagram.com/"]');
                    if (userLinks.length > 0) {
                        var m = userLinks[0].getAttribute('href').match(/instagram\\.com\\/([^/]+)/);
                        if (m) username = m[1];
                    }
                }
                // Like count from aria-label
                var likeCount = 0;
                var spans = parent ? parent.querySelectorAll('span') : [];
                for (var j = 0; j < spans.length; j++) {
                    var label = spans[j].getAttribute('aria-label') || '';
                    var lm = label.match(/([0-9,.KkMm]+)\s*like/i);
                    if (lm) likeCount = lm[1];
                }
                results.push({url: 'https://www.instagram.com' + href, username: username, likes: likeCount});
            });
            return JSON.stringify(results);
        })()"""

        raw = self._cdp.evaluate(extract_js, timeout=10)
        if raw:
            try:
                import json
                posts = json.loads(raw)
                for p in posts:
                    url = p.get('url', '')
                    if url and url not in {x['url'] for x in all_posts}:
                        all_posts.append(p)
                        uname = p.get('username', '')
                        if uname and uname not in ('explore', 'reels', 'direct', 'accounts', 'p'):
                            all_usernames.add(uname)
            except (json.JSONDecodeError, TypeError):
                pass

        # Scroll down
        scroll_js = "window.scrollBy(0, window.innerHeight * 0.8)"
        self._cdp.evaluate(scroll_js, timeout=5)

        delay = self._config.scroll_delay()
        time.sleep(delay)
        scrolls_done += 1
        self._session.profile_views_used += 0  # No budget cost for passive scrolling

    return {
        'posts': all_posts,
        'usernames': list(all_usernames),
        'scrolls_done': scrolls_done,
    }
```

**Step 2: Write test**

```python
def test_browse_feed_extracts_posts():
    """browse_feed returns posts and usernames from feed."""
    # Mock CDP to return post links
    ...
```

**Step 3: Run tests**

Run: `cd ~/projects/igautomation && source .venv/bin/activate && pytest tests/test_behavior_engine.py -v`

**Step 4: Commit**

```bash
git add src/igautomation/behavior/engine.py tests/test_behavior_engine.py
git commit -m "feat: add browse_feed method for passive feed harvesting"
```

---

### Task 2: Add `browse_reels` strategy to BehaviorEngine

**Objective:** New method that swipes through the Reels tab, extracting each reel's URL, username, caption, and metadata. Reels are the highest-value browsing activity — algorithmically curated, infinite content.

**Files:**
- Modify: `src/igautomation/behavior/engine.py`
- Test: `tests/test_behavior_engine.py`

**Step 1: Add `browse_reels` method**

Add after `browse_feed`:

```python
def browse_reels(self, max_reels: int = 20) -> dict:
    """Swipe through the Reels tab, extracting reel URLs and metadata.

    Navigates to /reels/ first, then swipes down through reels.
    Returns dict: reels (list of dicts), scrolls_done (int)
    """
    self._delay()

    # Navigate to Reels tab
    self._cdp.navigate('https://www.instagram.com/reels/', wait=4)
    time.sleep(2)

    all_reels = []
    scrolls_done = 0

    for i in range(max_reels):
        if self._session.is_exhausted():
            break
        if not self._session.can_view_reel():
            break

        # Extract current reel data
        extract_js = """(function() {
            var r = {};
            // Current reel URL from browser location or article
            var links = document.querySelectorAll('a[href*="/reel/"]');
            if (links.length > 0) {
                r.url = 'https://www.instagram.com' + links[links.length - 1].getAttribute('href');
            }
            // Username from the reel header
            var userLinks = document.querySelectorAll('a[href*="instagram.com/"]');
            for (var i = 0; i < userLinks.length; i++) {
                var href = userLinks[i].getAttribute('href') || '';
                var m = href.match(/instagram\\.com\\/([^/?/]+)/);
                if (m && m[1] && !['reel','reels','p','explore','direct'].includes(m[1])) {
                    r.username = m[1];
                    break;
                }
            }
            // Caption / first line of text
            var spans = document.querySelectorAll('span[dir="auto"]');
            for (var i = 0; i < spans.length; i++) {
                var t = spans[i].textContent.trim();
                if (t.length > 20 && t.length < 500) {
                    r.caption = t;
                    break;
                }
            }
            // Views/likes from aria-labels
            var allSpans = document.querySelectorAll('span[aria-label]');
            for (var i = 0; i < allSpans.length; i++) {
                var label = allSpans[i].getAttribute('aria-label') || '';
                var vm = label.match(/([0-9,.KkMm]+)\s*(view|like)/i);
                if (vm) { r.views = vm[1]; break; }
            }
            return JSON.stringify(r);
        })()"""

        raw = self._cdp.evaluate(extract_js, timeout=10)
        if raw:
            try:
                import json
                reel = json.loads(raw)
                url = reel.get('url', '')
                if url and url not in {x.get('url', '') for x in all_reels}:
                    all_reels.append(reel)
            except (json.JSONDecodeError, TypeError):
                pass

        # Watch the reel for a realistic time (3-12 seconds)
        watch_time = random.uniform(3.0, 12.0)
        time.sleep(watch_time)
        self._session.reel_views_used += 1

        # Swipe down to next reel
        swipe_js = """
            (function() {
                // Swipe down: scroll main container or press Down arrow
                var containers = document.querySelectorAll('div[style*="overflow"]');
                if (containers.length > 0) {
                    containers[0].scrollBy(0, window.innerHeight);
                    return 'scrolled';
                }
                window.scrollBy(0, window.innerHeight);
                return 'window_scrolled';
            })()
        """
        self._cdp.evaluate(swipe_js, timeout=5)
        time.sleep(random.uniform(1.0, 3.0))

        scrolls_done += 1

    return {
        'reels': all_reels,
        'scrolls_done': scrolls_done,
    }
```

**Step 2: Write test**

```python
def test_browse_reels_extracts_reels():
    """browse_reels swipes through reels and returns metadata."""
    ...
```

**Step 3: Run tests**

Run: `cd ~/projects/igautomation && source .venv/bin/activate && pytest tests/test_behavior_engine.py -v`

**Step 4: Commit**

```bash
git add src/igautomation/behavior/engine.py tests/test_behavior_engine.py
git commit -m "feat: add browse_reels method for reel swiping + harvesting"
```

---

### Task 3: Add `browse_explore` strategy to BehaviorEngine

**Objective:** Browse the Explore tab for trending content outside the user's normal feed. High discovery value.

**Files:**
- Modify: `src/igautomation/behavior/engine.py`
- Test: `tests/test_behavior_engine.py`

**Step 1: Add `browse_explore` method**

```python
def browse_explore(self, max_scrolls: int = 10) -> dict:
    """Browse the Explore tab, extracting trending post URLs and usernames.

    Returns dict: posts (list of dicts), usernames (list of str), scrolls_done (int)
    """
    self._delay()

    self._cdp.navigate('https://www.instagram.com/explore/', wait=5)
    time.sleep(2)

    all_posts = []
    all_usernames = set()
    scrolls_done = 0

    for i in range(max_scrolls):
        if self._session.is_exhausted():
            break

        extract_js = """(function() {
            var results = [];
            var links = document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"], a[href*="/tv/"]');
            links.forEach(function(a) {
                var href = a.getAttribute('href') || '';
                var username = '';
                var parent = a.closest('div');
                if (parent) {
                    var userLinks = parent.querySelectorAll('a[href*="instagram.com/"]');
                    if (userLinks.length > 0) {
                        var m = userLinks[0].getAttribute('href').match(/instagram\\.com\\/([^/]+)/);
                        if (m) username = m[1];
                    }
                }
                results.push({url: 'https://www.instagram.com' + href, username: username});
            });
            return JSON.stringify(results);
        })()"""

        raw = self._cdp.evaluate(extract_js, timeout=10)
        if raw:
            try:
                import json
                posts = json.loads(raw)
                for p in posts:
                    url = p.get('url', '')
                    if url and url not in {x['url'] for x in all_posts}:
                        all_posts.append(p)
                        uname = p.get('username', '')
                        if uname and uname not in ('explore', 'reels', 'direct', 'accounts', 'p', 'tv'):
                            all_usernames.add(uname)
            except (json.JSONDecodeError, TypeError):
                pass

        scroll_js = "window.scrollBy(0, window.innerHeight * 0.8)"
        self._cdp.evaluate(scroll_js, timeout=5)
        time.sleep(self._config.scroll_delay())
        scrolls_done += 1

    return {
        'posts': all_posts,
        'usernames': list(all_usernames),
        'scrolls_done': scrolls_done,
    }
```

**Step 2-4: Test + commit** (same pattern as Task 1)

---

### Task 4: Add `feed_browsing` and `reel_browsing` daemon strategies

**Objective:** Wire the new BehaviorEngine methods into the daemon loop as first-class strategies. These become the PRIMARY strategies — everything else is secondary.

**Files:**
- Modify: `src/igautomation/daemon/loop.py` — add `_execute_feed_browsing` and `_execute_reel_browsing` methods
- Modify: `src/igautomation/daemon/strategies.py` — add to strategy list, update LLM prompt priorities, update FALLBACK_PLANS
- Test: `tests/test_daemon.py`

**Step 1: Add strategy executors in loop.py**

Add two new methods after `_execute_own_account_monitoring`:

```python
async def _execute_feed_browsing(
    self, cdp: CDPClient, graphql: GraphQLClient, engine: BehaviorEngine,
    db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
) -> None:
    """Browse the main feed like a real user — scroll, read, harvest posts, engage inline."""
    max_scrolls = plan.params.get("max_scrolls", 15)

    # Navigate to feed
    await asyncio.get_running_loop().run_in_executor(
        None, cdp.navigate, "https://www.instagram.com/", 5
    )
    await asyncio.sleep(2)

    # Browse and extract
    result = await asyncio.get_running_loop().run_in_executor(
        None, engine.browse_feed, max_scrolls
    )

    posts = result.get("posts", [])
    usernames = result.get("usernames", [])
    stats["actions_taken"] = result.get("scrolls_done", 0)

    # Save discovered posts to content_items
    for post in posts:
        url = post.get("url", "")
        if not url:
            continue
        try:
            content_type = "reel" if "/reel/" in url else "post" if "/p/" in url else "unknown"
            await db.upsert_content_item({
                "url": url,
                "content_type": content_type,
                "owner_username": post.get("username", ""),
                "engagement_status": "pending",
            })
        except Exception as e:
            logger.debug("Failed to save feed post %s: %s", url, e)

    # Save discovered usernames to accounts
    new_accounts = 0
    for username in usernames:
        try:
            existing = await db.get_account_by_username(username)
            if not existing:
                await db.upsert_account({"username": username})
                new_accounts += 1
        except Exception:
            pass

    stats["accounts_discovered"] = new_accounts
    stats["posts_harvested"] = len(posts)
    logger.info("Feed browsing: %d posts, %d usernames (%d new), %d scrolls",
                len(posts), len(usernames), new_accounts, result.get("scrolls_done", 0))

    # Inline engagement: like/save posts that meet criteria
    await self._inline_engagement(cdp, engine, db, posts, stats)


async def _execute_reel_browsing(
    self, cdp: CDPClient, graphql: GraphQLClient, engine: BehaviorEngine,
    db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
) -> None:
    """Swipe through reels like a real user — watch, harvest, engage inline."""
    max_reels = plan.params.get("max_reels", 20)

    result = await asyncio.get_running_loop().run_in_executor(
        None, engine.browse_reels, max_reels
    )

    reels = result.get("reels", [])
    stats["actions_taken"] = result.get("scrolls_done", 0)

    # Save discovered reels to content_items
    for reel in reels:
        url = reel.get("url", "")
        if not url:
            continue
        try:
            await db.upsert_content_item({
                "url": url,
                "content_type": "reel",
                "owner_username": reel.get("username", ""),
                "caption": reel.get("caption", ""),
                "engagement_status": "pending",
            })
        except Exception as e:
            logger.debug("Failed to save reel %s: %s", url, e)

    # Save discovered usernames
    new_accounts = 0
    for reel in reels:
        username = reel.get("username", "")
        if not username:
            continue
        try:
            existing = await db.get_account_by_username(username)
            if not existing:
                await db.upsert_account({"username": username})
                new_accounts += 1
        except Exception:
            pass

    stats["accounts_discovered"] = new_accounts
    stats["reels_harvested"] = len(reels)
    logger.info("Reel browsing: %d reels, %d new accounts, %d swipes",
                len(reels), new_accounts, result.get("scrolls_done", 0))

    # Inline engagement on watched reels
    await self._inline_engagement(cdp, engine, db,
        [{"url": r["url"], "username": r.get("username", "")} for r in reels], stats)
```

**Step 2: Add `_inline_engagement` helper**

```python
async def _inline_engagement(
    self, cdp: CDPClient, engine: BehaviorEngine,
    db: AsyncDatabaseStore, posts: list[dict], stats: dict,
) -> None:
    """Engage with harvested content inline — like/save/follow based on criteria."""
    max_inline_likes = 5
    max_inline_follows = 2
    likes_done = 0
    follows_done = 0

    for post in posts:
        if likes_done >= max_inline_likes and follows_done >= max_inline_follows:
            break
        if engine._session.is_exhausted():
            break

        url = post.get("url", "")
        username = post.get("username", "")

        # Like criteria: random ~20% chance (real users don't like everything)
        if likes_done < max_inline_likes and engine.can_like() and random.random() < 0.2:
            try:
                liked = await asyncio.get_running_loop().run_in_executor(
                    None, engine.like_post, url
                )
                if liked:
                    likes_done += 1
                    stats["actions_taken"] += 1
                    # Update content engagement status
                    try:
                        await db.update_content_engagement_status_by_url(url, "engaged")
                    except Exception:
                        pass
            except Exception:
                pass

        # Follow criteria: if username exists and ~10% chance
        if follows_done < max_inline_follows and username and engine.can_follow() and random.random() < 0.1:
            try:
                followed = await asyncio.get_running_loop().run_in_executor(
                    None, engine.follow_user, username
                )
                if followed:
                    follows_done += 1
                    stats["actions_taken"] += 1
                    account = await db.get_account_by_username(username)
                    if account:
                        await db.log_interaction(account["id"], "follow", username, self._current_session_id)
            except Exception:
                pass
```

**Step 3: Add `feed_browsing` and `reel_browsing` to match/case block**

In `_run_one_session`, add before `case "discovery"`:

```python
case "feed_browsing":
    await self._execute_feed_browsing(
        cdp, graphql, engine, db, plan, stats
    )
case "reel_browsing":
    await self._execute_reel_browsing(
        cdp, graphql, engine, db, plan, stats
    )
case "explore_browsing":
    await self._execute_explore_browsing(
        cdp, graphql, engine, db, plan, stats
    )
```

Also add `_execute_explore_browsing` (same pattern as feed_browsing but calling `engine.browse_explore`).

**Step 4: Update strategies.py**

Add to strategy list in `DaemonConfig`:
- `feed_browsing`, `reel_browsing`, `explore_browsing`

Update `FALLBACK_PLANS` — prioritize browsing:
- 5 `feed_browsing`, 4 `reel_browsing`, 2 `explore_browsing`, 2 `profiling`, 2 `engagement`, 1 `content_engagement`, 1 `discovery`, 1 `monitoring`

Update LLM prompt to emphasize browsing as primary activity.

**Step 5: Add `update_content_engagement_status_by_url` to store.py**

```python
async def update_content_engagement_status_by_url(self, url: str, status: str) -> None:
    await self.db.execute(
        "UPDATE content_items SET engagement_status = ?, updated_at = ? WHERE url = ?",
        (status, _now(), url),
    )
    await self.db.commit()
```

**Step 6: Test + commit**

Run: `cd ~/projects/igautomation && source .venv/bin/activate && pytest tests/ -v`

---

### Task 5: Increase session frequency and adjust timing

**Objective:** Make the daemon run more sessions per day with shorter gaps, mimicking a real user who checks IG throughout the day.

**Files:**
- Modify: `src/igautomation/daemon/strategies.py` — increase `max_sessions_per_day`, adjust session durations
- Modify: `src/igautomation/behavior/config.py` — increase reel/story view caps

**Step 1: Update DaemonConfig defaults**

```python
max_sessions_per_day: int = 16  # was 8
skip_probability: float = 0.05  # was 0.15 — less skipping
session_duration_min: int = 120  # was 300 — shorter sessions (2-8 min)
session_duration_max: int = 480
```

**Step 2: Update BehaviorConfig — increase reel budget**

```python
session_reel_views_max: int = 30  # was 10 — reels are primary now
session_story_views_max: int = 10  # was 5
session_profile_views_max: int = 15  # was 8
session_likes_max: int = 20  # was 10
daily_likes_max: int = 80  # was 40
daily_follows_max: int = 20  # was 10
daily_profile_views_max: int = 50  # was 30
```

**Step 3: Update SessionScheduler — shorter gaps**

In scheduler defaults:
```python
min_gap_minutes: int = 15  # was 30
max_gap_minutes: int = 45  # was 90
```

**Step 4: Test + commit**

---

### Task 6: Add `upsert_content_item` helper + schema migration for feed harvesting

**Objective:** Ensure `content_items` table can be upserted efficiently by URL (primary key for dedup). Add missing `owner_username`, `caption` fields if not present.

**Files:**
- Modify: `src/igautomation/db/store.py` — verify/update `upsert_content_item`
- Modify: `src/igautomation/db/schema.py` — add migration if needed

**Step 1: Verify `upsert_content_item` handles all fields**

Check that it INSERTs or UPDATEs by URL, and includes `owner_username`, `caption`, `hashtags`, `shortcode`, `engagement_status` in both INSERT and UPDATE paths.

**Step 2: Add migration 006 for any missing columns**

```sql
ALTER TABLE content_items ADD COLUMN owner_username TEXT;
ALTER TABLE content_items ADD COLUMN shortcode TEXT;
ALTER TABLE content_items ADD COLUMN caption TEXT;
ALTER TABLE content_items ADD COLUMN hashtags TEXT;
```

Use `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` pattern or try/except.

**Step 3: Test + commit**

---

### Task 7: Fix the "stuck running" session from May 19

**Objective:** Clean up the ghost session and add a session cleanup on daemon startup.

**Files:**
- Modify: `src/igautomation/daemon/loop.py` — add startup cleanup
- Direct: SQL to fix the stuck session

**Step 1: Fix the stuck session directly**

```sql
UPDATE sessions SET status = 'error', ended_at = datetime('now') WHERE status = 'running' AND ended_at IS NULL;
```

**Step 2: Add cleanup in `_run_forever_async`**

After DB init in the main loop, clean up stale sessions:

```python
# Clean up stale "running" sessions from previous daemon runs
await db.db.execute(
    """UPDATE sessions SET status = 'error', ended_at = ?
    WHERE status = 'running' AND ended_at IS NULL
    AND started_at < datetime('now', '-1 hour')""",
    (_now(),),
)
await db.db.commit()
```

**Step 3: Test + commit**

---

### Task 8: Make LLM planner prioritize browsing strategies

**Objective:** Update the LLM planning prompt so feed/reel browsing are always the top-priority strategies. Discovery and profiling should only be chosen when the DB has very few accounts or lots of stale data.

**Files:**
- Modify: `src/igautomation/daemon/strategies.py` — update `llm_planning_prompt`

**Step 1: Rewrite the strategy priority section of the prompt**

Key changes:
- `feed_browsing` and `reel_browsing` are listed as **primary** activities
- Add hint: "A real user checks their feed and reels throughout the day. Browsing should be the DEFAULT strategy."
- Only pick `profiling` when unanalyzed > 100 AND it's been >3 sessions since last profiling
- Only pick `discovery` when total_accounts < 200
- `content_engagement` only when pending > 200 AND it's been >5 sessions since last

**Step 2: Update `_gather_stats` to include browsing-relevant stats**

Add:
- `content_harvested_today` — count of content_items added today
- `reels_watched_today` — count from interaction_log where action_type starts with 'reel'

**Step 3: Test + commit**

---

### Task 9: Update CLI to support new strategies

**Objective:** Allow `igx session --strategy feed_browsing` and `igx session --strategy reel_browsing`.

**Files:**
- Modify: `src/igautomation/cli.py` — update session command help text

**Step 1: Update session strategy choices**

Add `feed_browsing`, `reel_browsing`, `explore_browsing` to the strategy option help text and validation.

**Step 2: Test manually**

```bash
igx session --strategy feed_browsing --db igautomation.db
```

**Step 3: Commit**

---

### Task 10: End-to-end integration test

**Objective:** Verify the whole pipeline works: daemon starts → picks feed_browsing/reel_browsing → harvests posts/reels → saves to DB → inline engagement works.

**Files:**
- Test: `tests/test_integration_browsing.py`

**Step 1: Write integration test**

Mock CDP to return realistic feed/reel HTML. Verify:
1. `browse_feed` extracts post URLs
2. `browse_reels` extracts reel URLs
3. Daemon saves them to content_items
4. Inline engagement likes some posts
5. Session stats are correct

**Step 2: Run full test suite**

```bash
cd ~/projects/igautomation && source .venv/bin/activate && pytest tests/ -v
```

**Step 3: Manual smoke test with real Chrome**

Start daemon briefly:
```bash
igx daemon start --db igautomation.db --background --verbose
# Wait 2-3 minutes
igx daemon status
igx db stats
```

Verify content_items and accounts grew.

**Step 4: Final commit**

```bash
git add -A
git commit -m "feat: active browsing overhaul — feed/reel/explore strategies, inline engagement, higher frequency"
```

---

## Summary of Changes

| Area | Before | After |
|------|--------|-------|
| Primary activity | Discovery (GraphQL cascade) | Feed scrolling + reel swiping |
| Sessions/day | 8 max | 16 max |
| Session gap | 30-90 min | 15-45 min |
| Skip probability | 15% | 5% |
| Reel views/session | 10 | 30 |
| Content harvesting | Manual CSV load only | Automatic from feed/reels |
| Engagement model | Separate "engagement" sessions | Inline while browsing |
| Stale sessions | Never cleaned up | Auto-cleaned on startup |
| LLM planning | Treats all strategies equally | Prioritizes browsing |
| Explore tab | Never used | Browsed for trending content |
