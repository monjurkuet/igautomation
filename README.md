# igautomation

Instagram automation, exploration, and content-processing framework built on Chrome DevTools Protocol.

**How it works**: igautomation connects to an already-running Chrome browser via `--remote-debugging-port=9224`, then uses CDP to execute JavaScript and `fetch()` calls inside the logged-in Instagram session. That gives it access to Instagram's internal APIs without separate login handling.

## Quick Start

### 1. Launch Chrome with remote debugging

```bash
# macOS
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9224

# Windows (from WSL)
/mnt/c/Program\ Files/Google/Chrome/Application/chrome.exe --remote-debugging-port=9224

# Linux
google-chrome --remote-debugging-port=9224
```

Log into Instagram in that Chrome window.

### 2. Install igautomation

```bash
cd ~/projects/igautomation
uv sync
```

### 3. Use it

```bash
# List Chrome tabs (verify connection)
igx tabs

# Discover 100+ Bangladeshi model accounts from seed profiles
igx discover z.subha_ anonna_fatima --count 100

# Search Instagram users
igx search "bangladeshi model" --count 50

# Get suggested/similar accounts for a profile
igx suggest z.subha_

# Analyze previously collected accounts
igx analyze --input output/accounts.json
```

## Architecture

```
src/igautomation/
├── analysis/            # LLM analysis and strategy evaluation
├── behavior/            # Human-like browsing, rate limiting, session config
├── cdp/                 # Chrome DevTools Protocol client + tab discovery
├── content/              # Content loading, analysis, and engagement models
├── daemon/              # Long-running orchestration loop and scheduling
├── db/                  # Async SQLite schema and store
├── graphql/              # Instagram internal API client
├── scraper/              # Account discovery and profile analysis
├── storage/             # Legacy JSON/CSV/SQLite export helpers
├── cli.py               # Main Typer app (`igx`)
├── cli_content.py       # Content/collections CLI subcommands
├── import_accounts.py   # Import helper for account datasets
└── migrate.py           # Database migration helper
```

## Discovery Strategies

| Strategy | How it works | Speed | Quality |
|---|---|---|---|
| `existing_tabs` | Scrape profile links from open Chrome tabs | ⚡ Instant | Low |
| `shoutout_pages` | Visit BD shoutout pages, scroll, collect links | 🐢 Slow | Medium |
| `graphql_suggestions` | Query IG's "Suggested for you" API | ⚡ Fast | High |
| `search` | IG user search API | ⚡ Fast | Medium |
| `hashtags` | Visit hashtag pages, collect from posts | 🐢 Slow | Medium |
| `cascade` | For each account found, fetch THEIR suggestions | ⚡ Fast | Very High |

**Recommended**: `graphql_suggestions,cascade` — these two strategies combined can find 200+ related accounts in under 2 minutes.

## Programmatic Usage

```python
from igautomation.cdp import CDPClient, TabDiscovery
from igautomation.scraper import AccountCollector
from igautomation.scraper.analyzer import ProfileAnalyzer
from igautomation.storage import JSONStore, SQLiteStore

# Connect to Chrome
tab = TabDiscovery.find_ig_tab()
cdp = CDPClient()
cdp.connect(tab["webSocketDebuggerUrl"])

# Discover accounts
collector = AccountCollector(cdp)
accounts = collector.collect(
    seed_usernames=["z.subha_", "anonna_fatima"],
    target_count=100,
    strategies=["graphql_suggestions", "cascade"],
)

# Analyze profiles
analyzer = ProfileAnalyzer(cdp)
profiles = analyzer.analyze(accounts)

# Save results
store = JSONStore()
store.save(ProfileAnalyzer.to_dicts(profiles))

db = SQLiteStore()
db.upsert_accounts(ProfileAnalyzer.to_dicts(profiles))

cdp.close()
```

## Output

All results are saved to `./output/` by default:
- `accounts.json` — Full account data with metadata
- `accounts.csv` — Spreadsheet-friendly export
- `igautomation.db` — SQLite database for querying

## Requirements

- Python 3.11+
- Chrome with `--remote-debugging-port=9224`
- Instagram logged into that Chrome session
