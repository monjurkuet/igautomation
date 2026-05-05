"""SQL DDL and migration helpers for the igautomation database."""

SCHEMA_SQL = """
-- Core accounts table
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    user_id TEXT,
    full_name TEXT,
    bio TEXT,
    profile_pic_url TEXT,
    is_private INTEGER DEFAULT 0,
    is_verified INTEGER DEFAULT 0,
    follower_count INTEGER,
    following_count INTEGER,
    post_count INTEGER,
    category TEXT,
    tier TEXT,
    relevance_score REAL DEFAULT 0.0,
    is_active INTEGER DEFAULT 1,
    first_seen_at TEXT DEFAULT (datetime('now')),
    last_checked_at TEXT DEFAULT (datetime('now')),
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- How we discovered each account
CREATE TABLE IF NOT EXISTS discovery_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    strategy TEXT NOT NULL,
    source_username TEXT,
    query_text TEXT,
    discovered_at TEXT DEFAULT (datetime('now'))
);

-- Every organic action we take
CREATE TABLE IF NOT EXISTS interaction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES accounts(id),
    action_type TEXT NOT NULL,
    detail TEXT,
    session_id TEXT,
    performed_at TEXT DEFAULT (datetime('now'))
);

-- Follower count snapshots for growth tracking
CREATE TABLE IF NOT EXISTS follower_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    follower_count INTEGER NOT NULL,
    following_count INTEGER,
    post_count INTEGER,
    snapshot_at TEXT DEFAULT (datetime('now'))
);

-- Daemon session tracking
CREATE TABLE IF NOT EXISTS sessions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 session_uuid TEXT UNIQUE NOT NULL,
 strategy TEXT DEFAULT 'discovery',
 started_at TEXT DEFAULT (datetime('now')),
 ended_at TEXT,
 actions_taken INTEGER DEFAULT 0,
 accounts_discovered INTEGER DEFAULT 0,
 status TEXT DEFAULT 'running'
);

-- LLM analysis results
CREATE TABLE IF NOT EXISTS analysis_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES accounts(id),
    analysis_type TEXT NOT NULL,
    prompt_summary TEXT,
    result TEXT,
    model_used TEXT,
    analyzed_at TEXT DEFAULT (datetime('now'))
);
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_accounts_username ON accounts(username);
CREATE INDEX IF NOT EXISTS idx_accounts_tier ON accounts(tier);
CREATE INDEX IF NOT EXISTS idx_accounts_category ON accounts(category);
CREATE INDEX IF NOT EXISTS idx_accounts_relevance ON accounts(relevance_score DESC);
CREATE INDEX IF NOT EXISTS idx_discovery_account ON discovery_events(account_id);
CREATE INDEX IF NOT EXISTS idx_discovery_strategy ON discovery_events(strategy);
CREATE INDEX IF NOT EXISTS idx_interaction_account ON interaction_log(account_id);
CREATE INDEX IF NOT EXISTS idx_interaction_type ON interaction_log(action_type);
CREATE INDEX IF NOT EXISTS idx_follower_account ON follower_snapshots(account_id);
CREATE INDEX IF NOT EXISTS idx_sessions_uuid ON sessions(session_uuid);
CREATE INDEX IF NOT EXISTS idx_analysis_account ON analysis_log(account_id);
"""

# Named migrations for future schema evolution.
MIGRATIONS: list[tuple[str, str]] = [
    ("001_initial", SCHEMA_SQL + INDEXES_SQL),
]
