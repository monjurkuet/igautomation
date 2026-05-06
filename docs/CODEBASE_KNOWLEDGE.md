---
name: igautomation
description: Instagram automation framework via Chrome DevTools Protocol. Connect to logged-in Chrome on port 9224, execute JS/fetch for IG GraphQL APIs.
version: 0.1.0
project_path: ~/projects/igautomation
cli: igx
---

# igautomation — Codebase Knowledge

## Quick Reference

- **Path**: `~/projects/igautomation`
- **Python**: 3.11, uv-managed `.venv/`
- **Build**: hatchling, entry point `igx = igautomation.cli:app`
- **Git**: remote origin/main (monjurkuet/igautomation)
- **Deps**: websocket-client, requests, typer, rich, aiosqlite, pydantic, croniter, pyyaml; dev: pytest, pytest-asyncio, ruff
- **Testing**: `pytest` with `asyncio_mode = "auto"` in pyproject.toml

## Architecture

```
src/igautomation/
├── analysis/
│   ├── __init__.py
│   └── analyzer.py # AnalysisEngine — LLM-driven data quality & strategy
├── behavior/
│   ├── __init__.py
│   ├── config.py # BehaviorConfig (Pydantic v2) + SessionConfig (dataclass)
│   ├── engine.py # BehaviorEngine — wraps CDP with human-like timing/caps
│   └── rate_limiter.py # RateLimiter — exponential backoff + jitter
├── cdp/
│   ├── client.py # CDPClient — short-lived WS per command
│   └── discovery.py # TabDiscovery — find Chrome tabs via /json
├── daemon/
│   ├── __init__.py
│   ├── loop.py # DaemonLoop — LLM-orchestrated daemon main loop
│   ├── strategies.py # DaemonConfig + 4 strategy functions
│   └── scheduler.py # SessionScheduler — human-like daily session patterns
├── db/
│   ├── __init__.py
│   ├── schema.py # SQL DDL, indexes, migrations list
│   └── store.py # AsyncDatabaseStore — aiosqlite async CRUD
├── graphql/
│   └── client.py # GraphQLClient — IG internal APIs
├── scraper/
│   ├── collector.py # AccountCollector — 6 discovery strategies
│   └── analyzer.py # ProfileAnalyzer — verify & enrich profiles
├── storage/
│   └── store.py # JSONStore, CSVStore, SQLiteStore (legacy export helpers still used by CLI)
├── cli.py # `igx` CLI (typer app)
├── cli_content.py # content/collections CLI group
├── import_accounts.py # bulk importer for account data
├── migrate.py # Migrator — old schema → new DB
└── __init__.py
```

## CLI Commands

```
igx tabs                                    # List Chrome tabs
igx discover z.subha_ --count 100           # Discover accounts via cascade
igx search "bangladeshi model"              # Search IG users
igx suggest z.subha_                        # Get suggested accounts
igx analyze --input output/accounts.json    # Enrich & verify profiles
```

## Module Details

### cdp/client.py (417 lines) — `CDPClient`
- Short-lived WebSocket connections per CDP command (avoids Chrome killing long-lived)
- `connect(ws_url, origin="chrome://inspect")` — stores WS URL
- `evaluate(js, timeout=20)` — `Runtime.evaluate` with `returnByValue`, `awaitPromise`
- `navigate(url, wait=4)` — `Page.navigate` then `time.sleep(wait)`
- `scroll(max_scrolls=10, delay=2.0)` — scrolls page, collects IG profile links; early-stops if 0 new after 3+ scrolls
- `click_see_all()` — finds and clicks "See all" elements
- `SKIP_USERNAMES` — 35+ non-profile IG path segments
- `_USERNAME_RE` — regex for valid IG usernames

### cdp/discovery.py (103 lines) — `TabDiscovery`
- All static methods
- `list_tabs(base_url="http://localhost:9224")` — GET `/json`, filters iframe tabs
- `find_ig_tab(base_url, url_pattern="instagram.com")`
- `get_ig_tabs(base_url)`

### graphql/client.py (333 lines) — `GraphQLClient`
- Takes `CDPClient` in constructor
- `_csrf_token()` — reads `csrftoken` cookie from browser
- `_fetch_graphql(doc_id, friendly_name, variables, endpoint="/graphql/query")` — POST via `fetch()` with proper headers
- `get_suggested_users(target_user_id)` — DOC_SUGGESTED_PRELOAD query
- `get_suggested_users_lazy(target_user_id)` — DOC_SUGGESTED_LAZY query
- `search_users(query, count=50)` — topsearch API, returns `{username, pk, full_name, is_verified, profile_pic_url}`
- `get_user_id(username)` — web_profile_info API first, then fallback navigate + page regex
- `get_profile_meta(username)` — navigates to profile, reads og:description
- `get_discover_people()` — /api/v1/web/discover/people/

**Constants**:
- `DOC_SUGGESTED_PRELOAD = "25814188068245954"`
- `DOC_SUGGESTED_LAZY = "25878289415125440"`
- `DOC_PROFILE_CONTENT = "25858451687162830"` (kept for completeness; verify usage before deleting)
- `IG_APP_ID = "936619743392459"`

**Helper**: `_extract_usernames(data, depth=0)` — recursive walker for `username` fields in nested JSON (max depth 20)

### scraper/collector.py (535 lines) — `AccountCollector`
- Takes `CDPClient`, optional `GraphQLClient`
- `accounts` property → `set[str]`, `user_ids` property → `dict[str, str]`
- `on_progress(callback)` — register progress callbacks

**6 Discovery Strategies**:
1. `scrape_existing_tabs(base_url)` — reads existing Chrome tabs
2. `scrape_shoutout_pages(pages, max_per_page=12)` — hard limit 500 accounts
3. `fetch_suggestions(usernames)` — resolves user IDs → GraphQL suggestions
4. `scrape_hashtags(hashtags, max_scrolls=8)` — scrolls hashtag pages
5. `search_users(queries)` — topsearch API per query
6. `cascade_suggestions(max_depth=2, max_profiles=50, target_count=0)` — exponential graph expansion, only individual accounts as seeds

- `collect(seed_usernames, target_count=100, strategies)` — master orchestrator
- `get_sorted()` — returns sorted list
- `_is_individual_account(username)` — filters out hub/shoutout pages by keyword
- `json_imports(raw)` — local JSON import helper used during CSV/JSON loading

**Constants**: `BD_SHOUTOUT_PAGES` (60 pages), `BD_HASHTAGS` (21), `BD_SEARCH_TERMS` (10)

### scraper/analyzer.py (191 lines) — `ProfileAnalyzer` + `ProfileInfo`
- **ProfileInfo** (dataclass): username, url, exists, full_name, meta_description, follower_count, following_count, post_count, bio, is_bd, is_model, bd_keywords_matched, model_keywords_matched
- `analyze(usernames, skip_existing=True, known_good)` → `list[ProfileInfo]`
- `_analyze_one(username)` — navigates to profile, reads og:description, parses counts, checks BD/model keywords
- `_parse_meta_counts(info)` — regex for "101K Followers, 342 Following, 852 Posts"
- `filter_bd_models(profiles)` — filters `is_bd or is_model`

**Constants**: `BD_KEYWORDS` (17 items), `MODEL_KEYWORDS` (14 items)

### storage/store.py (213 lines)
- **JSONStore**: save/load, default output dir `./output/`
- **CSVStore**: save, dynamic fieldnames
- **SQLiteStore**: accounts + user_ids tables, upsert, get_all_accounts, get_bd_models, count

### cli.py (366 lines) — Typer app `igx`
- `tabs(port=9224)` — Rich table of Chrome IG tabs
- `discover(seeds, count=100, port=9224, output, strategies, analyze=True, verbose)` — full pipeline: collect → analyze → save JSON/CSV/SQLite → Rich table
- `search(query, count=50, port=9224, verbose)` — user search
- `suggest(username, port=9224, verbose)` — profile suggestions
- `analyze(input_file, port=9224, verbose)` — analyze from JSON

## Known Issues & Code Smells

1. **`DOC_PROFILE_CONTENT` dead code** — defined in graphql/client.py but never used
2. **`ProfileInfo.bio` never populated** — field exists but `_analyze_one()` never extracts bio
3. **Missing `json` top-level import in collector.py** — uses local `import json as _json` inside `json_imports()`
4. **`upsert_accounts` count is misleading** — counts all processed, not truly new inserts
5. ~~**No rate-limiting / anti-detection**~~ — **FIXED**: BehaviorEngine + RateLimiter with exponential backoff (Phase 6 complete).
6. **`navigate()` uses `time.sleep()`** — no DOM-ready or network-idle event detection
7. **Manual JS string escaping** in `_fetch_graphql()` — fragile, potential injection risk
8. **Hard-coded 500-account limit** in `scrape_shoutout_pages()` — not configurable
9. **Keyword matching false positives** — short keywords like "bd", "ctg" could match unintended text
10. ~~**No tests**~~ — **FIXED**: 98 tests passing across 6 test files
11. ~~**No venv**~~ — **FIXED**: uv venv at .venv/ with all deps
12. ~~**SQLite no `updated_at`**~~ — **FIXED**: AsyncDatabaseStore has proper timestamps
13. ~~**No indexing**~~ — **FIXED**: 11 indexes on the new schema

## Chrome Debug Port Conventions

- **Port 9224** — dedicated to igautomation (IG-logged-in Chrome instance)
- **Port 9222** — user's main daily-driver Chrome with Facebook (and other services) already logged in. **Always prefer port 9222 for browser_navigate tasks** when the user asks to browse Facebook or other sites where they're already logged in. Do NOT use Hermes's isolated browser for these tasks.
- When restarting Chrome, always use `--remote-debugging-port=<PORT>`.
- Chrome's debug port can get overwhelmed from aggressive cascade (too many rapid WS connections). Fix: `get_user_id` now uses web_profile_info API instead of page scraping (faster, no navigation).
- **WSL2 mirrored mode**: CDP ports must use `localhost` (not `127.0.0.1`) from WSL — `localhost` routes via iphlpsvc to Windows; `127.0.0.1` goes to WSL loopback (no CDP). Always verify with `curl http://localhost:PORT/json/version`.

## What's Verified Working

- ✅ igx tabs — lists Chrome tabs
- ✅ igx suggest z.subha_ — 38 suggestions
- ✅ igx search "bangladeshi model" — 5 results via topsearch
- ✅ Discovery cascade — 827+ accounts in <10 min from 2 seeds
- ✅ GraphQL suggestions API — 30-75 new accounts per profile

## Working Conventions

- User prefers `uv` for Python project management
- User prefers aggressive refactors over backward compatibility
- User approves all commands permanently
- User prefers natural/human-sounding writing
- Git: monjurkuet, SSH preferred

## New Modules (Phase 0-2, implemented)

### behavior/config.py — `BehaviorConfig` + `SessionConfig`

- **BehaviorConfig** (Pydantic v2 BaseModel): tunable params — action_delay_min/max, scroll_delay_min/max, session_duration_min/max, per-session caps (likes, follows, profile_views, reel_views, searches), daily caps, read_dwell_min/max, session_cooldown_min/max. Methods: `action_delay()`, `scroll_delay()`, `read_dwell()`, `cooldown_seconds()`, `new_session()`.
- **SessionConfig** (dataclass): mutable per-session state — `duration_seconds`, max_*/used_* counters, `started_at` (uses `time.monotonic()`). Methods: `can_like()`, `can_follow()`, `can_view_profile()`, `can_view_reel()`, `can_search()`, `time_remaining()`, `is_exhausted()`.

### behavior/engine.py — `BehaviorEngine`

- Wraps `CDPClient` with human-like timing, session + daily budgets.
- Private attrs: `_cdp`, `_config`, `_session`, `_daily_likes`, `_daily_follows`, `_daily_profile_views`.
- Methods: `scroll_feed(max_scrolls)`, `view_profile(username)`, `like_post(post_url)`, `follow_user(username)`, `search_and_browse(query, graphql)`, `watch_reel(reel_url)`, `run_session_loop(actions)`.
- Budget checks: `can_like()`, `can_follow()`, `can_view_profile()` — check both session and daily.

### db/schema.py — SQL DDL

- 6 tables: `accounts`, `discovery_events`, `interaction_log`, `follower_snapshots`, `sessions`, `analysis_log`.
- 11 indexes on username, tier, category, relevance_score, strategy, action_type, session_uuid, etc.
- `MIGRATIONS` list for future schema evolution.

### db/store.py — `AsyncDatabaseStore`

- All methods are `async`, backed by `aiosqlite`.
- Key methods: `initialize()`, `close()`, `upsert_account(data) -> id`, `get_account_by_username()`, `get_accounts_by_tier()`, `get_unanalyzed_accounts()`, `add_discovery_event()`, `get_discovery_stats()`, `log_interaction()`, `add_follower_snapshot()`, `create_session()`, `end_session()`, `add_analysis()`.
- Uses `aiosqlite.Row` factory for dict-like access.
- Datetimes stored as ISO-8601 UTC strings via `_now()` helper.

## Implementation Plan

Full plan at `docs/plans/2026-05-05-organic-ig-intelligence.md` — 7 phases, 14 tasks.

**All phases complete** ✅ — 98 tests passing, all 14 tasks done.

## New Modules (Phase 3-7, implemented)

### analysis/analyzer.py — `AnalysisEngine`

- Async LLM-driven analysis on collected data.
- Methods: `gather_stats()`, `run_data_quality()`, `run_strategy_optimization()`, `run_tier_adjustment()`.
- Returns `AnalysisResult` (summary, findings, recommendations, metrics).
- `save_result()` writes to `session_analyses` table via `AsyncDatabaseStore.add_session_analysis()`.

### behavior/rate_limiter.py — `RateLimiter`

- Async rate limiter with exponential backoff + jitter.
- `RateLimitConfig` (Pydantic): base_delay, max_delay, backoff_factor, jitter_fraction, cooldown_threshold, cooldown_duration.
- `RateLimiter.acquire()` — delays between calls, detects 429/rate-limit signals.
- `record_error(signal)` / `record_success()` — track consecutive errors, apply backoff.
- `RateLimitResponse` — wraps CDP/GraphQL responses, auto-detects rate-limit signals.
- Context manager support (`async with rate_limiter:`).
- Concurrency limiter via asyncio.Semaphore.

### daemon/loop.py — `DaemonLoop`

- LLM-driven daemon: `run_forever()` and `run_one(strategy)`.
- 4 strategies: discovery, profiling, monitoring, engagement.
- Each strategy uses BehaviorEngine for human-like actions.
- Session management: `create_session()`, `end_session()`, `get_status()`.
- Random skip probability (0.1) + cooldown jitter per strategy.

### daemon/strategies.py — `DaemonConfig` + strategy functions

- `DaemonConfig` (dataclass): db_path, behavior_config, strategies, session_duration, skip_probability, cooldown_range.
- `from_yaml(path)` classmethod for config files.
- Strategy functions: `run_discovery()`, `run_profiling()`, `run_monitoring()`, `run_engagement()`.

### daemon/scheduler.py — `SessionScheduler`

- Generates human-like daily session patterns.
- `SessionScheduleConfig` (Pydantic): waking hours, session counts, gap configs, cluster probability.
- `_generate_slots(day)` → list of datetime slots, weighted by activity periods.
- `_enforce_gaps()` — cluster logic (short gaps with probability) or enforce minimum gaps.
- `next_slot()` / `seconds_until_next()` / `peek_slots()` — for daemon integration.
- Day boundary enforcement (no sessions past midnight).

### migrate.py — `Migrator`

- Migrates from old SQLite (accounts + user_ids) + JSON exports to new schema.
- Maps `is_bd`/`is_model` → `category`/`tier` fields.
- Parses human-readable counts ("101K" → 101000).
- Auto-assigns tier: mega (100K+), macro (50K+), mid (10K+), micro (1K+), nano (<1K).
- CLI: `python -m igautomation.migrate [--dry-run]`

## Updated CLI Commands

```
igx tabs                    # List Chrome tabs
igx discover --seeds ...    # Discover accounts via cascade
igx search "query"          # Search IG users
igx suggest username        # Get suggested accounts
igx analyze --input file    # Enrich & verify profiles
igx session --strategy ...  # Run a single organic session
igx daemon start            # Start the daemon
igx daemon stop             # Stop the daemon
igx daemon status           # Show daemon status + DB stats
igx daemon analyze --type quality|strategy|tier  # Run LLM analysis
igx db stats                # Database statistics
igx db export               # Export to JSON
igx db migrate              # Migrate from old schema
```

## Hermes Cron

- **Job**: `igautomation-daily-analysis` (ID: 8c132b902732)
- **Schedule**: Daily at 9:00 AM (Asia/Dhaka)
- **Action**: Runs AnalysisEngine (data quality + strategy optimization) and reports to this Telegram thread

## Implementation Pitfalls

- **pytest-asyncio fixtures**: Must use `@pytest_asyncio.fixture` (NOT `@pytest.fixture`) for async fixtures, plus set `asyncio_mode = "auto"` in `[tool.pytest.ini_options]`. Otherwise async fixtures yield the generator object, not the resolved value.
- **Subagent timeouts**: Delegating medium-complexity file-writing tasks to subagents often times out (600s). Writing files directly is faster and more reliable for tasks under ~5 files.
- **Private attrs in subagent code**: When subagents write class code with private attrs (`_cdp`, `_session`), tests must reference those same names — not the public names you might assume.
- **`time.monotonic()` vs `time.time()`**: `SessionConfig.time_remaining()` uses `monotonic`. Tests that set `started_at = time.time() - X` will fail because monotonic and time epochs differ. Always use `time.monotonic()` in tests too.

## Reference Files

- `references/implementation-status.md` — phase-by-phase progress tracker, test counts, git commit log
