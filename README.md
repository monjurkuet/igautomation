# igautomation

Instagram automation, exploration, and scraping framework built on Chrome DevTools Protocol.

**How it works**: Instead of logging in separately or using unofficial APIs, igautomation connects to your already-running Chrome browser via the `--remote-debugging-port`. It executes JavaScript and `fetch()` calls inside your logged-in Instagram session, giving you full access to Instagram's internal GraphQL APIs with zero auth headaches.

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
pip install -e .
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
├── __init__.py          # Package version
├── cli.py               # Typer CLI (igx command)
├── cdp/
│   ├── __init__.py
│   ├── client.py        # CDPClient — WebSocket CDP commands
│   └── discovery.py     # TabDiscovery — find Chrome tabs
├── graphql/
│   ├── __init__.py
│   └── client.py        # GraphQLClient — IG internal API calls
├── scraper/
│   ├── __init__.py
│   ├── analyzer.py      # ProfileAnalyzer — verify & enrich accounts
│   └── collector.py     # AccountCollector — multi-strategy discovery
└── storage/
    ├── __init__.py
    └── store.py          # JSONStore, CSVStore, SQLiteStore
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
from igautomation.graphql import GraphQLClient
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
