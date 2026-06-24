# igautomation

Instagram automation, exploration, and content-processing framework built on Chrome DevTools Protocol.

**How it works**: igautomation connects to already-running Chrome browsers via CDP remote debugging ports (9222, 9224, 9225 by default), then uses CDP to execute JavaScript and `fetch()` calls inside the logged-in Instagram session. That gives it access to Instagram's internal APIs without separate login handling.

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
uv sync  # or `uv pip install -e .` if you prefer editable installs
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
navigate_ig.py              # 3-tier IG tab recovery (verify→navigate→createTarget)
src/igautomation/
├── analysis/               # LLM analysis and strategy evaluation
├── behavior/               # Human-like browsing, rate limiting, session config
├── cdp/                    # Chrome DevTools Protocol client + tab discovery
├── content/                # Content loading, analysis, and engagement models
├── daemon/                 # Long-running orchestration loop and scheduling
│   ├── loop.py              # DaemonLoop orchestrator (LLM-driven session runner)
│   ├── executors.py         # Per-strategy execution handlers (+ strategy registry)
│   ├── strategies.py        # DaemonConfig, FALLBACK_PLANS, SessionPlan
│   ├── scheduler.py         # Human-like session timing scheduler
│   ├── process.py           # PID file helpers, process liveness check
│   ├── cron_config.py       # Hermes cron integration (4 default jobs)
│   ├── service_config.py    # systemd user service renderer
│   └── account_prober.py    # CDP port/account probe
├── db/                     # Async SQLite schema, store, + migration tracking
│   ├── schema.py            # DDL, indexes, 5 named migrations (001-005)
│   └── store.py             # AsyncDatabaseStore — aiosqlite async CRUD
├── graphql/                # Instagram internal API client
├── scraper/                # Account discovery and profile analysis
├── storage/                # Legacy JSON/CSV/old-SQLite export helpers
├── llm_config.py           # Centralized LLM config from env/.env
├── migrate.py              # Data migration from old flat-file schema
├── import_accounts.py      # Bulk import accounts from JSON
├── cli.py                  # Main Typer app (`igx`)
├── cli_content.py          # Content/collections CLI subcommands
├── cli_daemon.py           # Daemon, cron, service CLI subcommands
├── cli_db.py               # Database CLI subcommands
└── cli_accounts.py         # Account management CLI subcommands
```

## Daemon Strategies

The daemon uses LLM-driven strategy selection from the strategy registry in `executors.py`. These are the session strategies — distinct from the old CLI discovery strategies.

### Implemented (12/12 registered)

| Strategy | Status | Description |
|---|---|---|
| `feed_browsing` | ✅ implemented | Scroll main feed, harvest posts, like/save inline |
| `reel_browsing` | ✅ implemented | Swipe through Reels tab, harvest reels, engage inline |
| `explore_browsing` | ✅ implemented | Browse trending/Explore tab |
| `discovery` | ✅ implemented | Run AccountCollector with sub-strategies (feed_browse, discover_people, etc.) |
| `profiling` | ✅ implemented | Batch-enrich unanalyzed accounts via ProfileAnalyzer |
| `monitoring` | ✅ implemented | Re-check follower counts for tracked accounts |
| `engagement` | ✅ implemented | Like/follow to maintain organic appearance |
| `content_engagement` | ✅ implemented | Browse, like, save, and LLM-analyze content items |
| `own_account_monitoring` | ✅ implemented | Snapshot own account follower counts |
| `story_viewing` | ⏳ not_implemented | Watch stories from followed accounts |
| `auto_unfollow` | ⏳ not_implemented | Unfollow non-reciprocal follows >7 days old |
| `comment_engagement` | ⏳ not_implemented | Leave genuine comments (disabled by default) |

**Recommended primary strategies**: `feed_browsing`, `reel_browsing`, `explore_browsing` — these mimic real user behavior and account for most sessions.

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

## Database & Migration

The async SQLite schema lives in `src/igautomation/db/schema.py`:

- **11 tables**: `accounts`, `discovery_events`, `interaction_log`, `follower_snapshots`, `sessions`, `analysis_log`, `content_items`, `content_engagement_log`, `collections`, `content_collections`, `ig_accounts`
- **Migration tracking**: `schema_migrations` table tracks applied migration names. Named migrations (`001_initial` through `005_ig_account_extras`) are listed in `schema.MIGRATIONS` and applied idempotently via `AsyncDatabaseStore.run_migrations()`.
- **Legacy import**: `python -m igautomation.migrate` migrates from old flat-file schema to current.

```bash
# Run pending migrations
igx db migrate

# Show DB stats
igx db stats

# Export to JSON
igx db export
```

## Output

All results saved to `./output/` by default:
- `accounts.json` — Full account data with metadata
- `accounts.csv` — Spreadsheet-friendly export
- `igautomation.db` — Main SQLite database (async, via aiosqlite)

## Requirements

- Python 3.11+
- Chrome with `--remote-debugging-port` (9222, 9224, or 9225)
- Instagram logged into that Chrome session

## Daemon

The daemon runs autonomous IG sessions with LLM-driven strategy selection:

```bash
# Start daemon in foreground
igx daemon start --foreground --db igautomation.db

# Start daemon in background (PID file managed)
igx daemon start --background --db igautomation.db

# Check status
igx daemon status

# Stop daemon
igx daemon stop
```

### Cron (4 default jobs)

List, install, and remove cron jobs for scheduled analysis. Managed block wrapped in `# BEGIN/END igautomation managed cron` markers:

| Job | Schedule | Description |
|---|---|---|
| `quality_analysis` | Every 6h | Quality review |
| `strategy_analysis` | Every 12h | Strategy optimization |
| `tier_analysis` | Daily 4AM UTC | Tier analysis |
| `db_export` | Weekly Sun 5AM UTC | Weekly backup export |

```bash
igx daemon cron-show            # Show configured jobs
igx daemon cron-next            # Show next run times
igx daemon cron-install         # Install managed block to crontab
igx daemon cron-uninstall       # Remove managed block
igx daemon cron-install --dry-run  # Preview without modifying
```

Crontab entries log to `logs/<job_name>.log` in the project directory and use `igx` CLI via `uv run`.

### Systemd

```bash
igx daemon service-show         # Show service file
igx daemon service-install      # Install to ~/.config/systemd/user/
igx daemon service-uninstall    # Remove service file
```

Default service: runs `igx daemon start --foreground --db igautomation.db`, auto-restarts on failure with 30s delay. Can be installed as a user service (`systemctl --user`) or system service (`/etc/systemd/system/`).

### IG Tab Navigation & Recovery

`navigate_ig.py` ensures every CDP port has a logged-in Instagram tab:

| Tier | Action | When |
|------|--------|------|
| 1 | Verify existing IG tab + login | IG tab already present |
| 2 | Navigate a real HTTP tab to IG | No IG tab but other tabs exist |
| 3 | `Target.createTarget` via browser-level WS | No page tabs at all |

Tier 3 uses the **browser-level** WebSocket URL (from `/json/version`), NOT a page-level WS — service workers and background pages can't issue browser-level CDP commands.

### Safe Defaults

- **Cooldown**: 5-30 min between sessions (configurable in `DaemonConfig`)
- **Sleep window**: no sessions 18:00-01:00 UTC (midnight-7am BDT)
- **Session skip**: 5% probability to skip any session (human-like)
- **Account cooldown**: 10 min between sessions on same account
- **Daily session limit**: 20-40 sessions/day (configurable)
- **Cluster probability**: 40% chance of tight session clusters (checking phone repeatedly)
- **Comment engagement**: disabled by default (`comment_enabled: false`)
