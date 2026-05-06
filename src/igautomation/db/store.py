"""AsyncDatabaseStore — async SQLite access layer via aiosqlite."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

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
        await self._db.executescript(SCHEMA_SQL + INDEXES_SQL)
        # Run any pending migrations
        for _name, migration_sql in MIGRATIONS:
            try:
                await self._db.executescript(migration_sql)
                await self._db.commit()
            except Exception:
                pass  # Already applied
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
        """Return accounts that have no entry in analysis_log."""
        cur = await self.db.execute(
            """
            SELECT a.* FROM accounts a
            LEFT JOIN analysis_log al ON a.id = al.account_id
            WHERE al.id IS NULL
            ORDER BY a.relevance_score DESC
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

    async def create_session(self, session_uuid: str, strategy: str = "discovery") -> int:
        """Start a new daemon session record."""
        cur = await self.db.execute(
            "INSERT INTO sessions (session_uuid, strategy) VALUES (?, ?)",
            (session_uuid, strategy),
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
        findings: str = "[]",
        recommendations: str = "[]",
        metrics: str = "{}",
        model_used: str | None = None,
    ) -> int:
        """Record a session-level (dashboard) LLM analysis result.

        Uses account_id=0 as a sentinel for session-level analyses
        (quality reviews, strategy optimization, tier analysis).
        """
        cur = await self.db.execute(
            """
            INSERT INTO analysis_log (account_id, analysis_type, prompt_summary, result, model_used)
            VALUES (?, ?, ?, ?, ?)
            """,
            (0, analysis_type, summary, json.dumps({
                "findings": findings,
                "recommendations": recommendations,
                "metrics": metrics,
            }) if not findings.startswith("[") else json.dumps({
                "findings": json.loads(findings) if isinstance(findings, str) else findings,
                "recommendations": json.loads(recommendations) if isinstance(recommendations, str) else recommendations,
                "metrics": json.loads(metrics) if isinstance(metrics, str) else metrics,
            }), model_used),
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
        self, content_item_id: int, collection_id: int
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
        except Exception:
            # Already exists — ignore
            return 0
