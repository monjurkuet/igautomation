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
- **Python**: 3.11, editable install (no venv — global)
- **Build**: hatchling, entry point `igx = igautomation.cli:app`
- **Git**: 1 commit on `main`, clean tree, remote origin/main
- **Deps**: websocket-client, requests, typer, rich; dev: pytest, ruff

## Architecture (12 modules)

```
src/igautomation/
├── cdp/
│   ├── client.py      # CDPClient — short-lived WS per command
│   └── discovery.py   # TabDiscovery — find Chrome tabs via /json
├── graphql/
│   └── client.py      # GraphQLClient — IG internal APIs
├── scraper/
│   ├── collector.py   # AccountCollector — 6 discovery strategies
│   └── analyzer.py    # ProfileAnalyzer — verify & enrich profiles
├── storage/
│   └── store.py       # JSONStore, CSVStore, SQLiteStore
├── cli.py             # `igx` CLI (typer app)
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
- `DOC_PROFILE_CONTENT = "25858451687162830"` (DEFINED BUT NEVER USED — dead code)
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
- `json_imports(raw)` — local `import json` workaround (see issues)

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
5. **No rate-limiting / anti-detection** — only `time.sleep(0.3)` between API calls; no backoff, no 429 handling
6. **`navigate()` uses `time.sleep()`** — no DOM-ready or network-idle event detection
7. **Manual JS string escaping** in `_fetch_graphql()` — fragile, potential injection risk
8. **Hard-coded 500-account limit** in `scrape_shoutout_pages()` — not configurable
9. **Keyword matching false positives** — short keywords like "bd", "ctg" could match unintended text
10. **No tests** — pytest listed as dev dep but no test files exist
11. **No venv** — installed globally in system Python
12. **SQLite no `updated_at`** — REPLACE silently overwrites with no timestamp
13. **No indexing** beyond PRIMARY KEY — `get_bd_models()` does full table scan

## Chrome Debug Port Issue

Chrome's debug port can get overwhelmed from aggressive cascade (too many rapid WS connections). Fix: `get_user_id` now uses web_profile_info API instead of page scraping (faster, no navigation). When restarting Chrome, always use `--remote-debugging-port=9224`.

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
