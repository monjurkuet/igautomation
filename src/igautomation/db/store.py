"""AsyncDatabaseStore — async SQLite access layer via aiosqlite."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import sqlite3

import aiosqlite

from igautomation.db.schema import SCHEMA_SQL, INDEXES_SQL, MIGRATIONS

logger = logging.getLogger(__name__)


def _now() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class AsyncDatabaseStore:
    """Async SQLite store for igautomation data.

    Parameters
    ----------
    db_path : str
        Path to the SQLite database file. Use ``:memory:`` for testing.
    """

    def __init__(self, db_path: str = "igautomation.db") -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Open the database and create tables if they don't exist."""
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await self._db.executescript(SCHEMA_SQL + INDEXES_SQL)
        # Migration tracking
        cur = await self.db.execute("SELECT name FROM schema_migrations")
        applied = {row[0] for row in await cur.fetchall()}
        if not applied:
            # Fresh DB — SCHEMA_SQL already includes all columns, mark everything applied
            for name, _ in MIGRATIONS:
                await self.db.execute(
                    "INSERT OR IGNORE INTO schema_migrations (name) VALUES (?)", (name,)
                )
            await self.db.commit()
        else:
            for name, migration_sql in MIGRATIONS:
                if name in applied:
                    continue
                try:
                    await self._db.executescript(migration_sql)
                    await self.db.execute(
                        "INSERT INTO schema_migrations (name) VALUES (?)", (name,)
                    )
                    await self.db.commit()
                except sqlite3.OperationalError:
                    pass
        logger.info("Database initialized at %s", self._db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._db

    # ------------------------------------------------------------------
    # accounts
    # ------------------------------------------------------------------

    async def upsert_account(self, data: dict[str, Any]) -> int:
        """Insert a new account or update an existing one by username.

        Returns the account id.
        """
        now = _now()
        username = data["username"]

        # Check if account exists
        cur = await self.db.execute(
            "SELECT id FROM accounts WHERE username = ?", (username,)
        )
        row = await cur.fetchone()

        if row:
            account_id = row[0]
            # Build UPDATE from provided fields
            update_fields = []
            update_values: list[Any] = []
            for key in (
                "user_id", "full_name", "bio", "profile_pic_url",
                "is_private", "is_verified", "follower_count",
                "following_count", "post_count", "category", "tier",
                "growth_rate", "growth_status",
                "relevance_score", "is_active",
            ):
                if key in data:
                    update_fields.append(f"{key} = ?")
                    update_values.append(data[key])
            update_fields.append("updated_at = ?")
            update_values.append(now)
            update_fields.append("last_checked_at = ?")
            update_values.append(now)
            update_values.append(account_id)

            await self.db.execute(
                f"UPDATE accounts SET {', '.join(update_fields)} WHERE id = ?",
                update_values,
            )
            await self.db.commit()
            return account_id

        # INSERT
        fields = ["username", "first_seen_at", "last_checked_at", "created_at", "updated_at"]
        values: list[Any] = [username, now, now, now, now]
        for key in (
            "user_id", "full_name", "bio", "profile_pic_url",
            "is_private", "is_verified", "follower_count",
            "following_count", "post_count", "category", "tier",
            "growth_rate", "growth_status", "relevance_score", "is_active",
        ):
            if key in data:
                fields.append(key)
                values.append(data[key])

        placeholders = ", ".join("?" for _ in values)
        cols = ", ".join(fields)
        cur = await self.db.execute(
            f"INSERT INTO accounts ({cols}) VALUES ({placeholders})", values
        )
        await self.db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_account_by_username(self, username: str) -> dict | None:
        """Return account dict by username, or None."""
        cur = await self.db.execute(
            "SELECT * FROM accounts WHERE username = ?", (username,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_accounts_by_tier(self, tier: str) -> list[dict]:
        """Return all accounts matching the given tier."""
        cur = await self.db.execute(
            "SELECT * FROM accounts WHERE tier = ? ORDER BY relevance_score DESC",
            (tier,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_unanalyzed_accounts(self, limit: int = 50) -> list[dict]:
        """Return accounts that have no follower_count (never profiled).

        Prioritizes accounts with no profile data, then stale accounts.
        """
        cur = await self.db.execute(
            """
            SELECT a.* FROM accounts a
            WHERE a.follower_count IS NULL
               AND (a.tier IS NULL OR a.tier != 'dead')
            ORDER BY a.id ASC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cur.fetchall()
        if rows:
            return [dict(r) for r in rows]
        # Fallback: accounts not checked in 24h
        cur = await self.db.execute(
            """
            SELECT a.* FROM accounts a
            WHERE a.last_checked_at IS NULL
               OR a.last_checked_at < datetime('now', '-1 day')
            ORDER BY a.last_checked_at ASC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # discovery_events
    # ------------------------------------------------------------------

    async def add_discovery_event(
        self,
        account_id: int,
        strategy: str,
        source_username: str | None = None,
        query_text: str | None = None,
    ) -> int:
        """Record how an account was discovered."""
        cur = await self.db.execute(
            """
            INSERT INTO discovery_events (account_id, strategy, source_username, query_text)
            VALUES (?, ?, ?, ?)
            """,
            (account_id, strategy, source_username, query_text),
        )
        await self.db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_discovery_stats(self) -> dict[str, int]:
        """Return count of discoveries per strategy."""
        cur = await self.db.execute(
            "SELECT strategy, COUNT(*) as cnt FROM discovery_events GROUP BY strategy"
        )
        rows = await cur.fetchall()
        return {r["strategy"]: r["cnt"] for r in rows}

    # ------------------------------------------------------------------
    # interaction_log
    # ------------------------------------------------------------------

    async def log_interaction(
        self,
        account_id: int | None,
        action_type: str,
        detail: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """Record an organic action taken."""
        cur = await self.db.execute(
            """
            INSERT INTO interaction_log (account_id, action_type, detail, session_id)
            VALUES (?, ?, ?, ?)
            """,
            (account_id, action_type, detail, session_id),
        )
        await self.db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # follower_snapshots
    # ------------------------------------------------------------------

    async def add_follower_snapshot(
        self,
        account_id: int,
        follower_count: int,
        following_count: int | None = None,
        post_count: int | None = None,
    ) -> int:
        """Record a follower count snapshot."""
        cur = await self.db.execute(
            """
            INSERT INTO follower_snapshots (account_id, follower_count, following_count, post_count)
            VALUES (?, ?, ?, ?)
            """,
            (account_id, follower_count, following_count, post_count),
        )
        await self.db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_follower_snapshots(
        self, account_id: int, limit: int = 30
    ) -> list[dict]:
        """Return follower snapshots for an account, oldest first."""
        cur = await self.db.execute(
            """
            SELECT follower_count, following_count, post_count, snapshot_at
            FROM follower_snapshots
            WHERE account_id = ?
            ORDER BY snapshot_at ASC
            LIMIT ?
            """,
            (account_id, limit),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def update_growth_status(
        self, account_id: int, growth_status: str, growth_rate: float
    ) -> None:
        """Update an account's growth_status and growth_rate."""
        await self.db.execute(
            """
            UPDATE accounts
            SET growth_status = ?, growth_rate = ?, updated_at = ?
            WHERE id = ?
            """,
            (growth_status, growth_rate, _now(), account_id),
        )
        await self.db.commit()

    async def refresh_growth_for_all(self) -> dict[str, int]:
        """Recompute growth_status for all accounts with >= 2 snapshots.

        Returns counts: {"rising": N, "stable": N, "declining": N, "unknown": N}.
        """
        from igautomation.scraper.analyzer import compute_growth_status

        counts: dict[str, int] = {"rising": 0, "stable": 0, "declining": 0, "unknown": 0}

        cur = await self.db.execute(
            """
            SELECT a.id, COUNT(fs.id) as snap_count
            FROM accounts a
            LEFT JOIN follower_snapshots fs ON a.id = fs.account_id
            GROUP BY a.id
            HAVING snap_count >= 2
            """
        )
        accounts = await cur.fetchall()

        for acct in accounts:
            account_id = acct["id"]
            snapshots = await self.get_follower_snapshots(account_id)
            snap_tuples = [(s["follower_count"], s["snapshot_at"]) for s in snapshots]
            status, rate = compute_growth_status(snap_tuples)
            await self.update_growth_status(account_id, status, rate)
            counts[status] += 1

        return counts

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------

    async def create_session(self, session_uuid: str, strategy: str = "discovery",
                           ig_account_id: int | None = None) -> int:
        """Start a new daemon session record."""
        cur = await self.db.execute(
            "INSERT INTO sessions (session_uuid, strategy, ig_account_id) VALUES (?, ?, ?)",
            (session_uuid, strategy, ig_account_id),
        )
        await self.db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def end_session(
        self,
        session_uuid: str,
        actions_taken: int = 0,
        accounts_discovered: int = 0,
        status: str = "completed",
    ) -> None:
        """End a daemon session."""
        now = _now()
        await self.db.execute(
            """
            UPDATE sessions
            SET ended_at = ?, actions_taken = ?, accounts_discovered = ?, status = ?
            WHERE session_uuid = ?
            """,
            (now, actions_taken, accounts_discovered, status, session_uuid),
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # analysis_log
    # ------------------------------------------------------------------

    async def add_analysis(
        self,
        account_id: int,
        analysis_type: str,
        result: str,
        model_used: str | None = None,
        prompt_summary: str | None = None,
    ) -> int:
        """Record a per-account LLM analysis result."""
        cur = await self.db.execute(
            """
            INSERT INTO analysis_log (account_id, analysis_type, prompt_summary, result, model_used)
            VALUES (?, ?, ?, ?, ?)
            """,
            (account_id, analysis_type, prompt_summary, result, model_used),
        )
        await self.db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def add_session_analysis(
        self,
        analysis_type: str,
        summary: str,
        findings: list[dict] | str = "[]",
        recommendations: list[dict] | str = "[]",
        metrics: dict | str = "{}",
        model_used: str | None = None,
    ) -> int:
        """Record a session-level (dashboard) LLM analysis result.

        Uses account_id=0 as a sentinel for session-level analyses
        (quality reviews, strategy optimization, tier analysis).

        Accepts findings/recommendations/metrics as either pre-serialized
        JSON strings OR native Python objects.
        """
        def _ensure_serialized(value: object) -> object:
            if not isinstance(value, str):
                return value
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value

        result_payload = {
            "findings": _ensure_serialized(findings),
            "recommendations": _ensure_serialized(recommendations),
            "metrics": _ensure_serialized(metrics),
        }

        cur = await self.db.execute(
            """
            INSERT INTO analysis_log (account_id, analysis_type, prompt_summary, result, model_used)
            VALUES (?, ?, ?, ?, ?)
            """,
            (0, analysis_type, summary, json.dumps(result_payload), model_used),
        )
        await self.db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # content_items
    # ------------------------------------------------------------------

    async def upsert_content_item(self, data: dict[str, Any]) -> int:
        """Insert or update a content item by URL. Returns the item id."""
        now = _now()
        url = data["url"]

        cur = await self.db.execute(
            "SELECT id FROM content_items WHERE url = ?", (url,)
        )
        row = await cur.fetchone()

        if row:
            item_id = row[0]
            update_fields = []
            update_values: list[Any] = []
            for key in (
                "shortcode", "content_type", "owner_username", "owner_id",
                "caption", "hashtags", "mentions", "media_type",
                "video_url", "video_view_count", "video_play_count",
                "like_count", "comment_count", "timestamp",
                "llm_analysis", "llm_collection_suggestion", "llm_tags",
                "is_bd_relevant", "content_niche", "priority",
                "category", "notes", "engagement_status",
            ):
                if key in data:
                    update_fields.append(f"{key} = ?")
                    update_values.append(data[key])
            update_fields.append("updated_at = ?")
            update_values.append(now)
            update_values.append(item_id)

            if update_fields:
                await self.db.execute(
                    f"UPDATE content_items SET {', '.join(update_fields)} WHERE id = ?",
                    update_values,
                )
                await self.db.commit()
            return item_id

        # INSERT
        fields = ["url", "first_seen_at", "created_at", "updated_at"]
        values: list[Any] = [url, now, now, now]
        for key in (
            "shortcode", "content_type", "owner_username", "owner_id",
            "caption", "hashtags", "mentions", "media_type",
            "video_url", "video_view_count", "video_play_count",
            "like_count", "comment_count", "timestamp",
            "llm_analysis", "llm_collection_suggestion", "llm_tags",
            "is_bd_relevant", "content_niche", "priority",
            "category", "notes", "engagement_status",
        ):
            if key in data:
                fields.append(key)
                values.append(data[key])

        placeholders = ", ".join("?" for _ in values)
        cols = ", ".join(fields)
        cur = await self.db.execute(
            f"INSERT INTO content_items ({cols}) VALUES ({placeholders})", values
        )
        await self.db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_content_item_by_url(self, url: str) -> dict | None:
        """Return content item dict by URL, or None."""
        cur = await self.db.execute(
            "SELECT * FROM content_items WHERE url = ?", (url,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_content_items_by_status(
        self, engagement_status: str = "pending", limit: int = 100
    ) -> list[dict]:
        """Return content items with the given engagement status."""
        cur = await self.db.execute(
            "SELECT * FROM content_items WHERE engagement_status = ? ORDER BY priority DESC, created_at ASC LIMIT ?",
            (engagement_status, limit),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_content_items_by_collection(
        self, collection_name: str, limit: int = 100
    ) -> list[dict]:
        """Return content items assigned to a named collection."""
        cur = await self.db.execute(
            """
            SELECT ci.* FROM content_items ci
            JOIN content_collections cc ON ci.id = cc.content_item_id
            JOIN collections c ON cc.collection_id = c.id
            WHERE c.name = ?
            ORDER BY ci.priority DESC
            LIMIT ?
            """,
            (collection_name, limit),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_content_stats(self) -> dict[str, Any]:
        """Return content engagement statistics."""
        stats: dict[str, Any] = {}

        cur = await self.db.execute("SELECT COUNT(*) as cnt FROM content_items")
        stats["total_items"] = (await cur.fetchone())["cnt"]

        cur = await self.db.execute(
            "SELECT engagement_status, COUNT(*) as cnt FROM content_items GROUP BY engagement_status"
        )
        stats["by_status"] = {r["engagement_status"]: r["cnt"] for r in await cur.fetchall()}

        cur = await self.db.execute(
            "SELECT content_type, COUNT(*) as cnt FROM content_items GROUP BY content_type"
        )
        stats["by_type"] = {r["content_type"]: r["cnt"] for r in await cur.fetchall()}

        cur = await self.db.execute(
            "SELECT content_niche, COUNT(*) as cnt FROM content_items WHERE content_niche != '' GROUP BY content_niche ORDER BY cnt DESC LIMIT 20"
        )
        stats["by_niche"] = {r["content_niche"]: r["cnt"] for r in await cur.fetchall()}

        cur = await self.db.execute(
            "SELECT llm_collection_suggestion, COUNT(*) as cnt FROM content_items WHERE llm_collection_suggestion != '' GROUP BY llm_collection_suggestion ORDER BY cnt DESC LIMIT 20"
        )
        stats["by_collection_suggestion"] = {
            r["llm_collection_suggestion"]: r["cnt"] for r in await cur.fetchall()
        }

        cur = await self.db.execute("SELECT COUNT(*) as cnt FROM collections")
        stats["total_collections"] = (await cur.fetchone())["cnt"]

        cur = await self.db.execute("SELECT COUNT(*) as cnt FROM content_engagement_log")
        stats["total_engagement_actions"] = (await cur.fetchone())["cnt"]

        cur = await self.db.execute(
            "SELECT action_type, status, COUNT(*) as cnt FROM content_engagement_log GROUP BY action_type, status"
        )
        rows = await cur.fetchall()
        action_stats: dict[str, dict[str, int]] = {}
        for r in rows:
            action_stats.setdefault(r["action_type"], {})[r["status"]] = r["cnt"]
        stats["engagement_actions"] = action_stats

        return stats

    # ------------------------------------------------------------------
    # content_engagement_log
    # ------------------------------------------------------------------

    async def log_content_engagement(
        self,
        content_item_id: int,
        action_type: str,
        status: str,
        detail: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """Record a content engagement action."""
        cur = await self.db.execute(
            """
            INSERT INTO content_engagement_log (content_item_id, action_type, status, detail, session_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (content_item_id, action_type, status, detail, session_id),
        )
        await self.db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def update_content_engagement_status(
        self, content_item_id: int, engagement_status: str
    ) -> None:
        """Update the engagement_status of a content item."""
        await self.db.execute(
            "UPDATE content_items SET engagement_status = ?, updated_at = ? WHERE id = ?",
            (engagement_status, _now(), content_item_id),
        )
        await self.db.commit()

    async def update_content_engagement_status_by_url(
        self, url: str, engagement_status: str
    ) -> None:
        """Update the engagement_status of a content item by URL."""
        await self.db.execute(
            "UPDATE content_items SET engagement_status = ?, updated_at = ? WHERE url = ?",
            (engagement_status, _now(), url),
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # collections
    # ------------------------------------------------------------------

    async def upsert_collection(
        self,
        name: str,
        collection_id: str | None = None,
        description: str = "",
        cover_media_id: str | None = None,
    ) -> int:
        """Insert or update a collection by name. Returns the collection id."""
        cur = await self.db.execute(
            "SELECT id FROM collections WHERE name = ?", (name,)
        )
        row = await cur.fetchone()

        if row:
            col_id = row[0]
            update_fields = []
            update_values: list[Any] = []
            if collection_id is not None:
                update_fields.append("collection_id = ?")
                update_values.append(collection_id)
            if description:
                update_fields.append("description = ?")
                update_values.append(description)
            if cover_media_id is not None:
                update_fields.append("cover_media_id = ?")
                update_values.append(cover_media_id)
            update_fields.append("updated_at = ?")
            update_values.append(_now())
            update_values.append(col_id)

            if update_fields:
                await self.db.execute(
                    f"UPDATE collections SET {', '.join(update_fields)} WHERE id = ?",
                    update_values,
                )
                await self.db.commit()
            return col_id

        cur = await self.db.execute(
            """
            INSERT INTO collections (name, collection_id, description, cover_media_id)
            VALUES (?, ?, ?, ?)
            """,
            (name, collection_id, description, cover_media_id),
        )
        await self.db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_all_collections(self) -> list[dict]:
        """Return all collections."""
        cur = await self.db.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM content_collections cc WHERE cc.collection_id = c.id) as item_count FROM collections c ORDER BY c.name"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_collection_by_name(self, name: str) -> dict | None:
        """Return collection by name."""
        cur = await self.db.execute(
            "SELECT * FROM collections WHERE name = ?", (name,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def add_content_to_collection(
        self,
        content_item_id: int,
        collection_id: int,
    ) -> int:
        """Add a content item to a collection. Returns the mapping id."""
        try:
            cur = await self.db.execute(
                """
                INSERT INTO content_collections (content_item_id, collection_id)
                VALUES (?, ?)
                """,
                (content_item_id, collection_id),
            )
            await self.db.commit()
            # Update item_count
            await self.db.execute(
                "UPDATE collections SET item_count = item_count + 1, updated_at = ? WHERE id = ?",
                (_now(), collection_id),
            )
            await self.db.commit()
            return cur.lastrowid  # type: ignore[return-value]
        except sqlite3.IntegrityError:
            return 0

    # ------------------------------------------------------------------
    # ig_accounts (our own IG accounts tracked per CDP port)
    # ------------------------------------------------------------------

    async def upsert_ig_account(self, data: dict[str, Any]) -> int:
        """Insert or update an IG account by port. Returns the ig_account id."""
        now = _now()
        port = data["port"]

        cur = await self.db.execute(
            "SELECT id FROM ig_accounts WHERE port = ?", (port,)
        )
        row = await cur.fetchone()

        if row:
            ig_id = row[0]
            update_fields = []
            update_values: list[Any] = []
            for key in (
                "username", "user_id", "full_name", "profile_pic_url",
                "is_private", "is_verified", "follower_count",
                "follower_snapshot_at", "status", "last_used_at",
                "daily_session_count", "daily_reset_at",
                "cooldown_until", "preferred_strategies", "warmup_complete",
            ):
                if key in data:
                    update_fields.append(f"{key} = ?")
                    update_values.append(data[key])
            update_fields.append("updated_at = ?")
            update_values.append(now)
            update_values.append(ig_id)

            if update_fields:
                await self.db.execute(
                    f"UPDATE ig_accounts SET {', '.join(update_fields)} WHERE id = ?",
                    update_values,
                )
                await self.db.commit()
            return ig_id

        # INSERT
        fields = ["port", "created_at", "updated_at"]
        values: list[Any] = [port, now, now]
        for key in (
            "username", "user_id", "full_name", "profile_pic_url",
            "is_private", "is_verified", "follower_count",
            "follower_snapshot_at", "status", "last_used_at",
            "daily_session_count", "daily_reset_at",
            "cooldown_until", "preferred_strategies", "warmup_complete",
        ):
            if key in data:
                fields.append(key)
                values.append(data[key])

        placeholders = ", ".join("?" for _ in values)
        cols = ", ".join(fields)
        cur = await self.db.execute(
            f"INSERT INTO ig_accounts ({cols}) VALUES ({placeholders})", values
        )
        await self.db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_ig_account_by_port(self, port: int) -> dict | None:
        """Return IG account dict by CDP port, or None."""
        cur = await self.db.execute(
            "SELECT * FROM ig_accounts WHERE port = ?", (port,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_all_ig_accounts(self) -> list[dict]:
        """Return all tracked IG accounts."""
        cur = await self.db.execute(
            "SELECT * FROM ig_accounts ORDER BY port"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_available_ig_accounts(self) -> list[dict]:
        """Return IG accounts eligible for daemon use (status = active or sleeping)."""
        cur = await self.db.execute(
            """SELECT * FROM ig_accounts
            WHERE status IN ('active', 'sleeping')
            ORDER BY CASE WHEN last_used_at IS NULL THEN 0 ELSE 1 END, last_used_at ASC, port ASC"""
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def update_ig_account_status(self, ig_account_id: int, status: str) -> None:
        """Update an IG account's status (active, sleeping, error, rate_limited)."""
        await self.db.execute(
            "UPDATE ig_accounts SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), ig_account_id),
        )
        await self.db.commit()

    async def touch_ig_account(self, ig_account_id: int) -> None:
        """Mark an IG account as just used (update last_used_at, bump daily count)."""
        now = _now()
        await self.db.execute(
            """UPDATE ig_accounts
            SET last_used_at = ?, daily_session_count = daily_session_count + 1, updated_at = ?
            WHERE id = ?""",
            (now, now, ig_account_id),
        )
        await self.db.commit()

    async def reset_daily_ig_accounts(self) -> None:
        """Reset daily_session_count for all IG accounts (called at day boundary)."""
        now = _now()
        await self.db.execute(
            "UPDATE ig_accounts SET daily_session_count = 0, daily_reset_at = ?, updated_at = ?",
            (now, now),
        )
        await self.db.commit()

    async def set_account_cooldown(self, ig_account_id: int, cooldown_seconds: int) -> None:
        """Set a cooldown on an IG account (e.g. after 429 rate limit)."""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        until = (now + timedelta(seconds=cooldown_seconds)).isoformat()
        await self.db.execute(
            "UPDATE ig_accounts SET cooldown_until = ?, status = 'rate_limited', updated_at = ? WHERE id = ?",
            (until, _now(), ig_account_id),
        )
        await self.db.commit()

    async def get_noncooled_ig_accounts(self) -> list[dict]:
        """Return IG accounts that are NOT in cooldown (active/sleeping + cooldown expired)."""
        now = _now()
        cur = await self.db.execute(
            """SELECT * FROM ig_accounts
            WHERE status IN ('active', 'sleeping')
            AND (cooldown_until IS NULL OR cooldown_until < ?)
            ORDER BY CASE WHEN last_used_at IS NULL THEN 0 ELSE 1 END, last_used_at ASC, port ASC""",
            (now,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_unfollow_candidates(self, grace_days: int = 7, limit: int = 20) -> list[dict]:
        """Return accounts we followed 7+ days ago that haven't followed back."""
        cur = await self.db.execute(
            """SELECT a.id, a.username, fl.performed_at as followed_at
            FROM accounts a
            JOIN interaction_log fl ON a.id = fl.account_id AND fl.action_type = 'follow'
            LEFT JOIN interaction_log fbl ON a.id = fbl.account_id AND fbl.action_type = 'follow_back'
            WHERE fbl.id IS NULL
            AND fl.performed_at < datetime('now', ? || ' days')
            AND a.is_active = 1
            ORDER BY fl.performed_at ASC
            LIMIT ?""",
            (f"-{grace_days}", limit),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_story_candidates(self, limit: int = 20) -> list[dict]:
        """Return accounts we follow (have follow interaction) for story viewing."""
        cur = await self.db.execute(
            """SELECT DISTINCT a.id, a.username
            FROM accounts a
            JOIN interaction_log il ON a.id = il.account_id AND il.action_type = 'follow'
            WHERE a.is_active = 1
            ORDER BY RANDOM()
            LIMIT ?""",
            (limit,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_comment_candidates(self, limit: int = 10) -> list[dict]:
        """Return content items we've already engaged with (good candidates for comments)."""
        cur = await self.db.execute(
            """SELECT ci.id, ci.url, ci.shortcode, ci.owner_username, ci.caption
            FROM content_items ci
            WHERE ci.engagement_status IN ('engaged', 'viewed')
            AND ci.url NOT IN (
                SELECT detail FROM interaction_log WHERE action_type = 'comment'
            )
            ORDER BY RANDOM()
            LIMIT ?""",
            (limit,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def snapshot_own_account(self, ig_account_id: int, follower_count: int,
                                    following_count: int = 0) -> None:
        """Update own IG account's follower snapshot."""
        now = _now()
        await self.db.execute(
            """UPDATE ig_accounts
            SET follower_count = ?, follower_snapshot_at = ?, updated_at = ?
            WHERE id = ?""",
            (follower_count, now, now, ig_account_id),
        )
        await self.db.commit()
