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
        await self._db.commit()
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
            "relevance_score", "is_active",
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
