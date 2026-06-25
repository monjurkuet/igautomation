"""SQL DDL and migration helpers for the igautomation database.

SCHEMA_SQL = current full schema. MIGRATIONS = transitional upgrades for old DBs.
"""

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
    growth_rate REAL DEFAULT 0.0,
    growth_status TEXT DEFAULT 'unknown',
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
    status TEXT DEFAULT 'running',
    ig_account_id INTEGER REFERENCES ig_accounts(id)
);

-- LLM analysis results
CREATE TABLE IF NOT EXISTS analysis_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    analysis_type TEXT NOT NULL,
    prompt_summary TEXT,
    result TEXT,
    model_used TEXT,
    analyzed_at TEXT DEFAULT (datetime('now'))
);

-- Content items for engagement tracking
CREATE TABLE IF NOT EXISTS content_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    shortcode TEXT,
    content_type TEXT DEFAULT 'unknown',
    owner_username TEXT,
    owner_id TEXT,
    caption TEXT,
    hashtags TEXT,
    mentions TEXT,
    media_type TEXT,
    video_url TEXT,
    video_view_count INTEGER,
    video_play_count INTEGER,
    like_count INTEGER,
    comment_count INTEGER,
    timestamp TEXT,
    -- LLM analysis fields
    llm_analysis TEXT,
    llm_collection_suggestion TEXT,
    llm_tags TEXT,
    is_bd_relevant INTEGER DEFAULT 0,
    content_niche TEXT,
    -- Priority and status
    priority INTEGER DEFAULT 5,
    category TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    engagement_status TEXT DEFAULT 'pending',
    first_seen_at TEXT DEFAULT (datetime('now')),
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Content engagement log (individual actions on content)
CREATE TABLE IF NOT EXISTS content_engagement_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_item_id INTEGER NOT NULL REFERENCES content_items(id),
    action_type TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    session_id TEXT,
    performed_at TEXT DEFAULT (datetime('now'))
);

-- Collections (IG Saved collections)
CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    collection_id TEXT,
    description TEXT DEFAULT '',
    cover_media_id TEXT,
    item_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Content-collection membership
CREATE TABLE IF NOT EXISTS content_collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_item_id INTEGER NOT NULL REFERENCES content_items(id),
    collection_id INTEGER NOT NULL REFERENCES collections(id),
    added_at TEXT DEFAULT (datetime('now')),
    UNIQUE(content_item_id, collection_id)
);

-- Migration tracking
CREATE TABLE IF NOT EXISTS schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-account IG accounts (multi-account support)
CREATE TABLE IF NOT EXISTS ig_accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  port INTEGER UNIQUE NOT NULL,
  username TEXT,
  user_id TEXT,
  full_name TEXT,
  profile_pic_url TEXT,
  is_private INTEGER DEFAULT 0,
  is_verified INTEGER DEFAULT 0,
  follower_count INTEGER DEFAULT 0,
  follower_snapshot_at TEXT,
  status TEXT DEFAULT 'active',
  last_used_at TEXT,
  daily_session_count INTEGER DEFAULT 0,
  daily_reset_at TEXT,
  cooldown_until TEXT,
  preferred_strategies TEXT,
  warmup_complete INTEGER DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_accounts_username ON accounts(username);
CREATE INDEX IF NOT EXISTS idx_accounts_tier ON accounts(tier);
CREATE INDEX IF NOT EXISTS idx_accounts_category ON accounts(category);
CREATE INDEX IF NOT EXISTS idx_accounts_relevance ON accounts(relevance_score DESC);
CREATE INDEX IF NOT EXISTS idx_accounts_growth_status ON accounts(growth_status);
CREATE INDEX IF NOT EXISTS idx_discovery_account ON discovery_events(account_id);
CREATE INDEX IF NOT EXISTS idx_discovery_strategy ON discovery_events(strategy);
CREATE INDEX IF NOT EXISTS idx_interaction_account ON interaction_log(account_id);
CREATE INDEX IF NOT EXISTS idx_interaction_type ON interaction_log(action_type);
CREATE INDEX IF NOT EXISTS idx_follower_account ON follower_snapshots(account_id);
CREATE INDEX IF NOT EXISTS idx_sessions_uuid ON sessions(session_uuid);
CREATE INDEX IF NOT EXISTS idx_analysis_account ON analysis_log(account_id);

CREATE INDEX IF NOT EXISTS idx_content_items_url ON content_items(url);
CREATE INDEX IF NOT EXISTS idx_content_items_type ON content_items(content_type);
CREATE INDEX IF NOT EXISTS idx_content_items_niche ON content_items(content_niche);
CREATE INDEX IF NOT EXISTS idx_content_items_engagement ON content_items(engagement_status);
CREATE INDEX IF NOT EXISTS idx_content_engagement_item ON content_engagement_log(content_item_id);
CREATE INDEX IF NOT EXISTS idx_content_engagement_type ON content_engagement_log(action_type);
CREATE INDEX IF NOT EXISTS idx_collections_name ON collections(name);
CREATE INDEX IF NOT EXISTS idx_content_collections_item ON content_collections(content_item_id);
CREATE INDEX IF NOT EXISTS idx_content_collections_collection ON content_collections(collection_id);
CREATE INDEX IF NOT EXISTS idx_ig_accounts_port ON ig_accounts(port);
CREATE INDEX IF NOT EXISTS idx_ig_accounts_status ON ig_accounts(status);
"""

# Named migrations for schema evolution.
MIGRATIONS: list[tuple[str, str]] = [
    ("001_initial", SCHEMA_SQL + INDEXES_SQL),
    (
        "002_growth_fields",
        """
        ALTER TABLE accounts ADD COLUMN growth_rate REAL DEFAULT 0.0;
        ALTER TABLE accounts ADD COLUMN growth_status TEXT DEFAULT 'unknown';
        CREATE INDEX IF NOT EXISTS idx_accounts_growth_status ON accounts(growth_status);
        """,
    ),
    (
        "003_content_tables",
        """
CREATE TABLE IF NOT EXISTS content_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT UNIQUE NOT NULL,
  shortcode TEXT,
  content_type TEXT DEFAULT 'unknown',
  owner_username TEXT,
  owner_id TEXT,
  caption TEXT,
  hashtags TEXT,
  mentions TEXT,
  media_type TEXT,
  video_url TEXT,
  video_view_count INTEGER,
  video_play_count INTEGER,
  like_count INTEGER,
  comment_count INTEGER,
  timestamp TEXT,
  llm_analysis TEXT,
  llm_collection_suggestion TEXT,
  llm_tags TEXT,
  is_bd_relevant INTEGER DEFAULT 0,
  content_niche TEXT,
  priority INTEGER DEFAULT 5,
  category TEXT DEFAULT '',
  notes TEXT DEFAULT '',
  engagement_status TEXT DEFAULT 'pending',
  first_seen_at TEXT DEFAULT (datetime('now')),
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS content_engagement_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content_item_id INTEGER NOT NULL REFERENCES content_items(id),
  action_type TEXT NOT NULL,
  status TEXT NOT NULL,
  detail TEXT,
  session_id TEXT,
  performed_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS collections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  collection_id TEXT,
  description TEXT DEFAULT '',
  cover_media_id TEXT,
  item_count INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS content_collections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content_item_id INTEGER NOT NULL REFERENCES content_items(id),
  collection_id INTEGER NOT NULL REFERENCES collections(id),
  added_at TEXT DEFAULT (datetime('now')),
  UNIQUE(content_item_id, collection_id)
);
CREATE INDEX IF NOT EXISTS idx_content_items_url ON content_items(url);
CREATE INDEX IF NOT EXISTS idx_content_items_type ON content_items(content_type);
CREATE INDEX IF NOT EXISTS idx_content_items_niche ON content_items(content_niche);
CREATE INDEX IF NOT EXISTS idx_content_items_engagement ON content_items(engagement_status);
CREATE INDEX IF NOT EXISTS idx_content_engagement_item ON content_engagement_log(content_item_id);
CREATE INDEX IF NOT EXISTS idx_content_engagement_type ON content_engagement_log(action_type);
CREATE INDEX IF NOT EXISTS idx_collections_name ON collections(name);
CREATE INDEX IF NOT EXISTS idx_content_collections_item ON content_collections(content_item_id);
CREATE INDEX IF NOT EXISTS idx_content_collections_collection ON content_collections(collection_id);
""",
    ),
    (
        "004_ig_accounts_and_session_link",
        """
CREATE TABLE IF NOT EXISTS ig_accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  port INTEGER UNIQUE NOT NULL,
  username TEXT,
  user_id TEXT,
  full_name TEXT,
  profile_pic_url TEXT,
  is_private INTEGER DEFAULT 0,
  is_verified INTEGER DEFAULT 0,
  follower_count INTEGER DEFAULT 0,
  follower_snapshot_at TEXT,
  status TEXT DEFAULT 'active',
  last_used_at TEXT,
  daily_session_count INTEGER DEFAULT 0,
  daily_reset_at TEXT,
  cooldown_until TEXT,
  preferred_strategies TEXT,
  warmup_complete INTEGER DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ig_accounts_port ON ig_accounts(port);
CREATE INDEX IF NOT EXISTS idx_ig_accounts_status ON ig_accounts(status);
ALTER TABLE sessions ADD COLUMN ig_account_id INTEGER REFERENCES ig_accounts(id);
""",
    ),
]
