"""Data storage and export — JSON, CSV, and SQLite backends."""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path.cwd() / "output"


class JSONStore:
    """Save and load account data as JSON files."""

    def __init__(self, output_dir: Path | str | None = None) -> None:
        self.output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        accounts: list[dict[str, Any]],
        filename: str = "accounts.json",
        extra: dict[str, Any] | None = None,
    ) -> Path:
        """Write accounts to a JSON file.

        Args:
            accounts: List of account dicts.
            filename: Output filename.
            extra: Additional metadata to include at the top level.

        Returns:
            Path to the written file.
        """
        path = self.output_dir / filename
        data: dict[str, Any] = {
            "total": len(accounts),
            "accounts": accounts,
            "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if extra:
            data.update(extra)

        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Saved %d accounts to %s", len(accounts), path)
        return path


class CSVStore:
    """Export account data as CSV."""

    def __init__(self, output_dir: Path | str | None = None) -> None:
        self.output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        accounts: list[dict[str, Any]],
        filename: str = "accounts.csv",
    ) -> Path:
        """Write accounts to a CSV file.

        All unique keys across all account dicts become columns.

        Returns:
            Path to the written file.
        """
        path = self.output_dir / filename
        if not accounts:
            path.write_text("", encoding="utf-8")
            return path

        # Collect all keys
        fieldnames: list[str] = []
        seen: set[str] = set()
        for acc in accounts:
            for key in acc:
                if key not in seen:
                    seen.add(key)
                    fieldnames.append(key)

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for acc in accounts:
                writer.writerow(acc)

        logger.info("Saved %d accounts to %s", len(accounts), path)
        return path


class SQLiteStore:
    """Persist account data in a local SQLite database."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            db_path = DEFAULT_OUTPUT_DIR / "igautomation.db"
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    username TEXT PRIMARY KEY,
                    url TEXT,
                    full_name TEXT,
                    meta_description TEXT,
                    follower_count TEXT,
                    following_count TEXT,
                    post_count TEXT,
                    bio TEXT,
                    is_bd INTEGER DEFAULT 0,
                    is_model INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_ids (
                    username TEXT PRIMARY KEY,
                    ig_user_id TEXT,
                    discovered_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.commit()

    def upsert_accounts(self, accounts: list[dict[str, Any]]) -> int:
        """Insert or update accounts. Returns number of new rows inserted."""
        inserted = 0
        with sqlite3.connect(self.db_path) as conn:
            for acc in accounts:
                try:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO accounts
                        (username, url, full_name, meta_description,
                         follower_count, following_count, post_count,
                         bio, is_bd, is_model)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            acc.get("username", ""),
                            acc.get("url", ""),
                            acc.get("full_name", ""),
                            acc.get("meta_description", acc.get("meta", "")),
                            acc.get("follower_count", ""),
                            acc.get("following_count", ""),
                            acc.get("post_count", ""),
                            acc.get("bio", ""),
                            int(acc.get("is_bd", False)),
                            int(acc.get("is_model", False)),
                        ),
                    )
                    inserted += 1
                except sqlite3.Error:
                    logger.warning("Failed to upsert @%s", acc.get("username", "?"))
            conn.commit()
        logger.info("Upserted %d accounts into SQLite", inserted)
        return inserted

    def save_user_ids(self, user_ids: dict[str, str]) -> int:
        """Save username -> user ID mappings."""
        inserted = 0
        with sqlite3.connect(self.db_path) as conn:
            for username, uid in user_ids.items():
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO user_ids (username, ig_user_id) VALUES (?, ?)",
                        (username, uid),
                    )
                    inserted += 1
                except sqlite3.Error:
                    pass
            conn.commit()
        return inserted

    def count(self) -> int:
        """Return total number of accounts."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()
            return row[0] if row else 0
