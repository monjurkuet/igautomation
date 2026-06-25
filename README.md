# igautomation

Instagram automation, exploration, and content-processing framework built on Chrome DevTools Protocol.

**How it works**: connects to already-running Chrome browsers via CDP remote debugging ports (9222, 9224, 9225), executes JavaScript and `fetch()` inside the logged-in Instagram session. No separate login flow — piggybacks on your authenticated Chrome.

## Quick Start

### 1. Launch Chrome with remote debugging

```bash
# Windows (from WSL)
/mnt/c/Program\ Files/Google/Chrome/Application/chrome.exe --remote-debugging-port=9224

# macOS
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9224

# Linux
google-chrome --remote-debugging-port=9224
```

Log into Instagram in that Chrome window.

### 2. Install

```bash
cd ~/projects/igautomation
uv sync
```

Set LLM credentials in `.env` or environment:
```env
OPENPAI_API_KEY=sk-...
OPENPAI_BASE_URL=https://llm.datasolved.org/v1
LLM_MODEL=groq/openai/gpt-oss-120b
```

### 3. Use it

```bash
# List Chrome tabs (verify connection)
igx tabs

# Discover Bangladeshi model accounts from seed profiles
igx discover z.subha_ anonna_fatima --count 100

# Search Instagram users
igx search "bangladeshi model" --count 50

# Get suggested/similar accounts
igx suggest z.subha_

# Analyze collected accounts
igx analyze --input accounts.json
```

## Architecture

```
src/igautomation/
├── analysis/               # LLM analysis and strategy evaluation engine
│   └── analyzer.py         # Prompt templates, LLM calling, result parsing
├── behavior/               # Human-like browsing simulation
│   ├── engine.py           # browse_feed, browse_reels, browse_explore, engagement
│   ├── config.py           # BehaviorConfig, SessionConfig (pydantic)
│   └── rate_limiter.py     # Per-action rate limiting with semaphore
├── cdp/                    # Chrome DevTools Protocol client
│   ├── client.py           # CDPClient: connect, evaluate, navigate, send/receive
│   └── discovery.py        # TabDiscovery: find IG tabs on CDP ports
├── content/                # Content processing pipeline
│   ├── analyzer.py         # Browser-based LLM content categorization
│   ├── engager.py          # Like/save actions on content
│   ├── loader.py           # Load/import content items from posts/reels
│   └── models.py           # ContentItem, EngagementStatus pydantic models
├── daemon/                 # Autonomous orchestration loop
│   ├── __main__.py         # Entry point (argparse)
│   ├── loop.py             # DaemonLoop: session orchestrator + cooldown
│   ├── executors.py        # Strategy executors (12 registered, 10 implemented)
│   ├── strategies.py       # DaemonConfig, SessionPlan, FALLBACK_PLANS
│   ├── scheduler.py        # Human-like session timing (clusters + gaps)
│   ├── process.py          # PID file management
│   ├── cron_config.py      # Hermes cron job definitions (4 jobs)
│   ├── service_config.py   # systemd service file renderer
│   └── account_prober.py   # CDP port → IG account resolution
├── db/                     # Async SQLite persistence
│   ├── schema.py           # DDL, 4 named migrations (001-004), 11 tables
│   └── store.py            # AsyncDatabaseStore — aiosqlite async CRUD
├── graphql/                # Instagram internal GraphQL API client
│   └── client.py           # fetch() via CDP, web profile info, suggestions
├── scraper/                # Account discovery and profile analysis
│   ├── analyzer.py         # ProfileAnalyzer: tier classification, growth status
│   └── collector.py        # AccountCollector: seed-based discovery cascade
├── storage/                # Legacy export helpers (JSON/CSV/sync-SQLite)
├── llm_config.py           # Centralized LLM config from env/.env
├── migrate.py              # Data migration: old flat-file → new async schema
├── import_accounts.py      # Bulk import accounts from JSON
├── cli.py                  # Main Typer app (`igx`)
├── cli_accounts.py         # Account management CLI
├── cli_content.py          # Content/collections CLI
├── cli_daemon.py           # Daemon, cron, service CLI
└── cli_db.py               # Database CLI (stats, export, migrate)
```

## Daemon

Autonomous IG session orchestration with LLM-driven strategy selection:

```bash
# Start daemon in foreground
igx daemon start --foreground

# Start daemon as background process
igx daemon start --background

# Status and control
igx daemon status
igx daemon stop
```

### Implemented Strategies (10/12 registered)

| Strategy | Status | Description |
|---|---|---|
| `feed_browsing` | ✅ | Scroll main feed, harvest posts, like/save inline |
| `reel_browsing` | ✅ | Swipe Reels, harvest, engage inline |
| `explore_browsing` | ✅ | Browse Explore tab |
| `discovery` | ✅ | AccountCollector with sub-strategies |
| `profiling` | ✅ | Batch-enrich unanalyzed accounts |
| `monitoring` | ✅ | Re-check follower counts |
| `engagement` | ✅ | Like/follow for organic appearance |
| `content_engagement` | ✅ | Browse, like, save, and LLM-analyze content |
| `own_account_monitoring` | ✅ | Snapshot own account stats |
| `story_viewing` | ⏳ | Stories from followed accounts |
| `auto_unfollow` | ⏳ | Unfollow non-reciprocal >7d old |
| `comment_engagement` | ⏳ | Comment (disabled by default) |

### Safe Defaults

- **Cooldown**: 5-30 min between sessions
- **Sleep window**: 18:00-01:00 UTC (midnight-7am BDT)
- **Session timeout**: 30 min max per session (kills hung CDP)
- **Daily sessions**: 20-40 per day (configurable)
- **Account cooldown**: 10 min between sessions on same account
- **LLM planning**: Auto-retries on bad JSON, falls back to random strategy

### Cron Jobs (4 defaults)

| Job | Schedule | Description |
|---|---|---|
| `quality_analysis` | Every 6h | Quality review |
| `strategy_analysis` | Every 12h | Strategy optimization |
| `tier_analysis` | Daily 4AM UTC | Tier analysis |
| `db_export` | Weekly Sun 5AM UTC | Backup export |

```bash
igx daemon cron-show            # Show configured jobs
igx daemon cron-next            # Next run times
igx daemon cron-install         # Install to crontab
igx daemon cron-uninstall       # Remove from crontab
```

### Systemd Service

```bash
igx daemon service-show         # Show service file
igx daemon service-install      # Install to ~/.config/systemd/user/
igx daemon service-uninstall    # Remove service file
```

## Database

Async SQLite via `aiosqlite`. 11 tables, migration-tracked:

```bash
igx db migrate         # Run pending migrations
igx db stats           # Show DB statistics  
igx db export          # Export to JSON
```

## Requirements

- Python 3.11+
- Chrome with `--remote-debugging-port` (9222, 9224, or 9225)
- Instagram logged into that Chrome session
- LLM API key for strategy planning and content analysis

## LLM Configuration

Loaded from environment or `.env` file by `llm_config.py`:

| Variable | Default | Purpose |
|---|---|---|
| `OPENPAI_API_KEY` | — | API key |
| `OPENPAI_BASE_URL` | `https://llm.datasolved.org/v1` | API endpoint |
| `LLM_MODEL` | `gemini-2.5-flash-lite` | Model name |

## Programmatic Usage

```python
from igautomation.cdp import CDPClient, TabDiscovery
from igautomation.scraper import AccountCollector
from igautomation.scraper.analyzer import ProfileAnalyzer
from igautomation.db.store import AsyncDatabaseStore

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

# Save to async DB
import asyncio
async def save():
    db = await AsyncDatabaseStore("igautomation.db")
    for p in profiles:
        await db.upsert_account(p.to_dict())
    await db.close()

asyncio.run(save())
cdp.close()
```
