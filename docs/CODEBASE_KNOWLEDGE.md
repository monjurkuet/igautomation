---
name: igautomation
description: Instagram automation framework via Chrome DevTools Protocol. Connect to logged-in Chrome on CDP ports 9222/9224/9225, execute JS/fetch for IG GraphQL APIs.
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
- **Testing**: `pytest` with `asyncio_mode = "auto"` in pyproject.toml. **180 tests** across 11 test files (test_analysis, test_behavior_config, test_behavior_engine, test_browsing_integration, test_daemon, test_daemon_e2e, test_daemon_process, test_db_schema, test_rate_limiter, test_scheduler, test_tier_expansion)

`navigate_ig.py` removed. 3-tier tab recovery logic now lives in the daemon loop directly.

## Architecture

```
src/igautomation/
├── analysis/
│   ├── __init__.py
│   └── analyzer.py               # AnalysisEngine — LLM-driven data quality & strategy
├── behavior/
│   ├── __init__.py
│   ├── config.py                  # BehaviorConfig (Pydantic v2) + SessionConfig
│   ├── engine.py                  # BehaviorEngine — wraps CDP with human-like timing
│   └── rate_limiter.py            # RateLimiter — exponential backoff + jitter
├── cdp/
│   ├── client.py                  # CDPClient — short-lived WS per command
│   └── discovery.py               # TabDiscovery — find Chrome tabs via /json
├── content/
│   ├── __init__.py
│   ├── loader.py                  # ContentLoader — fetch content items
│   ├── analyzer.py                # ContentAnalyzer — LLM content analysis
│   ├── engager.py                 # ContentEngager — like/save/view actions
│   └── models.py                  # ContentItem, ContentType, EngagementStatus
├── daemon/
│   ├── __init__.py
│   ├── __main__.py                # `python -m igautomation.daemon` entry (+ noisy logger suppression)
│   ├── loop.py                    # DaemonLoop — LLM-orchestrated main loop, 3-tier CDP recovery
│   ├── executors.py               # 12 strategy executors + strategy registry (543 lines)
│   ├── strategies.py              # DaemonConfig + FALLBACK_PLANS + SessionPlan
│   ├── scheduler.py               # SessionScheduler — human-like daily patterns
│   ├── process.py                 # PID file helpers, process liveness
│   ├── cron_config.py             # Cron config — 4 default jobs, render_crontab()
│   ├── service_config.py          # systemd service renderer (--verbose removed from ExecStart)
│   └── account_prober.py          # CDP port/account probe
├── db/
│   ├── __init__.py
│   ├── schema.py                  # SQL DDL, indexes, 5 named migrations (001-005)
│   └── store.py                   # AsyncDatabaseStore — aiosqlite async CRUD
├── graphql/
│   └── client.py                  # GraphQLClient — IG internal APIs
├── scraper/
│   ├── collector.py               # AccountCollector — 6 discovery strategies
│   └── analyzer.py                # ProfileAnalyzer — verify & enrich profiles
├── storage/
│   └── store.py                   # JSONStore, CSVStore, SQLiteStore (legacy)
├── llm_config.py                  # Centralized LLM config from env/.env
├── migrate.py                     # Data migration from old flat-file schema
├── import_accounts.py             # Bulk import accounts from JSON
├── cli.py                         # `igx` CLI (typer app), registers sub-apps
├── cli_content.py                 # content/collections CLI group
├── cli_daemon.py                  # daemon, cron, systemd CLI commands (341 lines)
├── cli_db.py                      # database CLI commands
├── cli_accounts.py                # account management CLI commands
├── __init__.py
```

## CLI Commands

```
igx tabs                                    # List Chrome tabs
igx discover z.subha_ --count 100           # Discover accounts via cascade
igx search "bangladeshi model"              # Search IG users
igx suggest z.subha_                        # Get suggested accounts
igx analyze --input output/accounts.json    # Enrich & verify profiles
igx session --strategy feed_browsing        # Run single daemon session

# Daemon
igx daemon start --foreground               # Start daemon (foreground)
igx daemon start --background               # Start daemon (background, PID)
igx daemon stop                             # Stop daemon by PID
igx daemon status                           # Show daemon status

# Cron (via igx daemon)
igx daemon cron-show                        # Show cron job configuration
igx daemon cron-next                        # Show next run times
igx daemon cron-install                     # Install crontab managed block
igx daemon cron-uninstall                   # Remove managed block
igx daemon cron-install --dry-run           # Preview install

# Systemd (via igx daemon)
igx daemon service-show                     # Show service file
igx daemon service-install                  # Install to ~/.config/systemd/user/
igx daemon service-uninstall                # Remove service file

# Database
igx db stats                                # Show database statistics
igx db export                               # Export accounts to JSON
igx db migrate                              # Run pending migrations

# Accounts
igx accounts list                           # List managed IG accounts
igx accounts add                            # Add IG account
igx accounts refresh                        # Refresh account data
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

### db/schema.py — Migration Tracking

4 named migrations in `MIGRATIONS` list:
- `001_initial` — full schema + indexes
- `002_growth_fields` — growth_rate, growth_status columns
- `003_content_tables` — content_items, content_engagement_log, collections, content_collections
- `004_ig_accounts_and_session_link` — ig_accounts table, session FK

`schema_migrations` table records which migrations have been applied. `AsyncDatabaseStore.run_migrations()` iterates `MIGRATIONS`, skips already-applied names, executes new ones atomically. Fresh DBs get all migrations applied at once.

### daemon/executors.py — Strategy Registry

`build_strategy_registry()` returns `dict[str, Callable]` mapping strategy names to executor functions. Registry covers all 12 strategies used in `FALLBACK_PLANS`:

| Strategy | Implemented | Notes |
|---|---|---|
| `feed_browsing` | ✅ | scroll main feed, harvest posts |
| `reel_browsing` | ✅ | swipe Reels tab |
| `explore_browsing` | ✅ | browse Explore tab |
| `discovery` | ✅ | AccountCollector with sub-strategies |
| `profiling` | ✅ | batch ProfileAnalyzer |
| `monitoring` | ✅ | re-check follower counts |
| `engagement` | ✅ | like/follow actions |
| `content_engagement` | ✅ | like/save + LLM-analyze content |
| `own_account_monitoring` | ✅ | snapshot own IG accounts |
| `story_viewing` | ❌ | no-op — `skipped_reason: "not_implemented"` |
| `auto_unfollow` | ❌ | no-op — `skipped_reason: "not_implemented"` |
| `comment_engagement` | ❌ | no-op when disabled (default) |

### cli.py — Typer app `igx`

## Known Issues & Code Smells

> **Fact check**: 180 tests passing across 11 test files (as of May 2026). The daemon refactor (loop.py → executors.py split, strategy registry, scheduler propagation, PID/cron/service modules) is complete.

1. **`DOC_PROFILE_CONTENT` dead code** — defined in graphql/client.py but never used
2. **`ProfileInfo.bio` never populated** — field exists but `_analyze_one()` never extracts bio
3. **Missing `json` top-level import in collector.py** — uses local `import json as _json` inside `json_imports()`
4. **`upsert_accounts` count is misleading** — counts all processed, not truly new inserts
5. **`navigate()` uses `time.sleep()`** — no DOM-ready or network-idle event detection
6. **Manual JS string escaping** in `_fetch_graphql()` — fragile, potential injection risk
7. **Hard-coded 500-account limit** in `scrape_shoutout_pages()` — not configurable
8. **Keyword matching false positives** — short keywords like "bd", "ctg" could match unintended text

### Fixed (strikethrough = done)

- ~~No rate-limiting / anti-detection~~ — BehaviorEngine + RateLimiter with exponential backoff
- ~~No tests~~ — 180 tests across 11 test files
- ~~No venv~~ — uv venv at .venv
- ~~SQLite no `updated_at`~~ — AsyncDatabaseStore has proper timestamps
- ~~No indexing~~ — 21 indexes on current schema
- ~~Old flat-file schema~~ — Async SQLite with 5 named migrations
- ~~No migration tracking~~ — `schema_migrations` table idempotent apply

## Chrome Debug Port Conventions

- **Port 9224** — igautomation IG-logged-in Chrome instance
- **Port 9222** — user's main daily-driver Chrome with Facebook (and other services) already logged in
- **Port 9225** — additional Chrome instance
| **All ports are Windows-hosted Chrome instances** — WSL2 accesses them via localhost forwarding. Managed by `sm-browser-watchdog` on Windows. Tab recovery logic lives in the daemon loop.

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

Full plan removed from docs/plans/. All 7 phases complete ✅.

## New Modules (Phase 3-7, implemented)

### analysis/analyzer.py — `AnalysisEngine`

- Async LLM-driven analysis on collected data.
- Methods: `run_quality_review()`, `run_strategy_optimization()`, `run_tier_analysis()`, `run_all().`
- Returns `AnalysisResult` (summary, findings, recommendations, metrics).
- `save_result()` writes to `analysis_log` via `AsyncDatabaseStore.add_session_analysis()`.

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

### daemon/strategies.py — `DaemonConfig` + `SessionPlan` + fallback plans

- `DaemonConfig` (Pydantic BaseModel): db_path, cdp_port, llm_base_url, llm_api_key, llm_model, max_sessions_per_day, sleep_hours_start/end, skip_session_probability, default_target_count, default_strategies, llm_enabled, llm_planning_prompt.
- `from_yaml(path)` classmethod for config files.
- `SessionPlan` stores `strategy`, `params`, and `rationale`; `FALLBACK_PLANS` provides default session plans when the LLM is unavailable.

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

## Hermes Cron

- **Job**: `igautomation-daily-analysis` (ID: 8c132b902732)
- **Schedule**: Daily at 9:00 AM (Asia/Dhaka)
- **Action**: Runs AnalysisEngine (data quality + strategy optimization) and reports to Telegram thread

## Implementation Pitfalls

- **pytest-asyncio fixtures**: Must use `@pytest_asyncio.fixture` (NOT `@pytest.fixture`) for async fixtures, plus set `asyncio_mode = "auto"` in `[tool.pytest.ini_options]`. Otherwise async fixtures yield the generator object, not the resolved value.
- **Subagent timeouts**: Delegating medium-complexity file-writing tasks to subagents often times out (600s). Writing files directly is faster and more reliable for tasks under ~5 files.
- **Private attrs in subagent code**: When subagents write class code with private attrs (`_cdp`, `_session`), tests must reference those same names — not the public names you might assume.
- **`time.monotonic()` vs `time.time()`**: `SessionConfig.time_remaining()` uses `monotonic`. Tests that set `started_at = time.time() - X` will fail because monotonic and time epochs differ. Always use `time.monotonic()` in tests too.

## Reference Files

- `references/implementation-status.md` — phase-by-phase progress tracker, test counts, git commit log
