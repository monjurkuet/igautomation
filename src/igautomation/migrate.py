"""Migrate data from the old flat-file/SQLite schema to the new database.

Old schema (output/igautomation.db):
  - accounts: username, url, full_name, meta_description, follower_count,
    following_count, post_count, bio, is_bd, is_model, created_at
  - user_ids: username, ig_user_id, discovered_at

New schema (src/igautomation/db/schema.py):
  - accounts, discovery_events, follower_snapshots, interactions,
    sessions, session_analyses

Usage::

    # Migrate from old DB + JSON files
    python -m igautomation.migrate --from-db output/igautomation.db \
                                   --from-json output/bd_models.json \
                                   --to igautomation.db

    # Dry run (show what would be migrated)
    python -m igautomation.migrate --dry-run --from-db output/igautomation.db
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sqlite3
from pathlib import Path

from igautomation.db.store import AsyncDatabaseStore

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Helper: parse follower counts like "101K", "1.2M"
# ------------------------------------------------------------------

_COUNT_RE = re.compile(r"([\d.]+)\s*([KMB]?)", re.IGNORECASE)
_MULTIPLIERS = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def parse_follower_count(raw: str | None) -> int | None:
    """Parse a human-readable follower count like '101K' → 101000."""
    if not raw:
        return None
    raw = raw.strip().replace(",", "")
    m = _COUNT_RE.match(raw)
    if not m:
        return None
    try:
        num = float(m.group(1))
        mult = _MULTIPLIERS.get(m.group(2).upper(), 1)
        return int(num * mult)
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------
# Migrator
# ------------------------------------------------------------------


class Migrator:
    """Migrate data from old schema to new."""

    def __init__(
        self,
        old_db_path: str,
        new_db_path: str,
        json_path: str | None = None,
    ) -> None:
        self.old_db_path = old_db_path
        self.new_db_path = new_db_path
        self.json_path = json_path
        self._stats: dict[str, int] = {}

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    async def run(self, dry_run: bool = False) -> dict[str, int]:
        """Run the migration.

        Returns stats dict with counts of migrated records.
        """
        self._stats = {
            "accounts_migrated": 0,
            "user_ids_migrated": 0,
            "json_accounts_migrated": 0,
            "discovery_events_created": 0,
        }

        # Read old data
        old_accounts = self._read_old_accounts()
        old_user_ids = self._read_old_user_ids()
        json_accounts = self._read_json_accounts()

        logger.info(
            "Old DB: %d accounts, %d user_ids | JSON: %d accounts",
            len(old_accounts),
            len(old_user_ids),
            len(json_accounts),
        )

        if dry_run:
            logger.info("Dry run — no data written")
            self._stats["accounts_migrated"] = len(old_accounts)
            self._stats["json_accounts_migrated"] = len(json_accounts)
            self._stats["user_ids_migrated"] = len(old_user_ids)
            return self.stats

        # Write to new DB
        new_db = AsyncDatabaseStore(self.new_db_path)
        await new_db.initialize()

        try:
            # Migrate old DB accounts
            for username, acct in old_accounts.items():
                followers = parse_follower_count(acct.get("follower_count"))
                following = parse_follower_count(acct.get("following_count"))
                posts = parse_follower_count(acct.get("post_count"))

                # Map old is_bd/is_model flags to category & tier
                category = None
                tier = None
                if bool(acct.get("is_model", 0)):
                    category = "model"
                if bool(acct.get("is_bd", 0)):
                    if category:
                        category = "bd_model"
                    else:
                        category = "bd"
                if followers is not None:
                    if followers >= 100_000:
                        tier = "mega"
                    elif followers >= 50_000:
                        tier = "macro"
                    elif followers >= 10_000:
                        tier = "mid"
                    elif followers >= 1_000:
                        tier = "micro"
                    else:
                        tier = "nano"

                account_id = await new_db.upsert_account({
                    "username": username,
                    "user_id": old_user_ids.get(username),
                    "full_name": acct.get("full_name", ""),
                    "bio": acct.get("bio", ""),
                    "follower_count": followers,
                    "following_count": following,
                    "post_count": posts,
                    "category": category,
                    "tier": tier,
                })
                self._stats["accounts_migrated"] += 1

                # Create discovery event
                await new_db.add_discovery_event(
                    account_id=account_id,
                    strategy="migration",
                    source_username=None,
                    query_text="imported from old schema",
                )
                self._stats["discovery_events_created"] += 1

            # Migrate JSON accounts (may overlap — upsert handles dedup)
            for acct in json_accounts:
                username = acct["username"]
                followers = parse_follower_count(acct.get("follower_count"))
                following = parse_follower_count(acct.get("following_count"))
                posts = parse_follower_count(acct.get("post_count"))

                category = None
                tier = None
                if bool(acct.get("is_model", False)):
                    category = "model"
                if bool(acct.get("is_bd", False)):
                    if category:
                        category = "bd_model"
                    else:
                        category = "bd"
                if followers is not None:
                    if followers >= 100_000:
                        tier = "mega"
                    elif followers >= 50_000:
                        tier = "macro"
                    elif followers >= 10_000:
                        tier = "mid"
                    elif followers >= 1_000:
                        tier = "micro"
                    else:
                        tier = "nano"

                account_id = await new_db.upsert_account({
                    "username": username,
                    "user_id": acct.get("user_id") or old_user_ids.get(username),
                    "full_name": acct.get("full_name", ""),
                    "bio": acct.get("bio", ""),
                    "follower_count": followers,
                    "following_count": following,
                    "post_count": posts,
                    "category": category,
                    "tier": tier,
                })
                self._stats["json_accounts_migrated"] += 1

                # Only add discovery event if this username wasn't already
                # handled in the old DB migration above
                if username not in old_accounts:
                    await new_db.add_discovery_event(
                        account_id=account_id,
                        strategy="migration",
                        source_username=None,
                        query_text="imported from JSON export",
                    )
                    self._stats["discovery_events_created"] += 1

        finally:
            await new_db.close()

        logger.info("Migration complete: %s", self.stats)
        return self.stats

    def _read_old_accounts(self) -> dict[str, dict]:
        """Read accounts from the old SQLite DB."""
        accounts: dict[str, dict] = {}
        if not Path(self.old_db_path).exists():
            logger.warning("Old DB not found: %s", self.old_db_path)
            return accounts

        conn = sqlite3.connect(self.old_db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM accounts").fetchall()
            for row in rows:
                d = dict(row)
                accounts[d["username"]] = d
        except sqlite3.OperationalError as e:
            logger.warning("Error reading old accounts: %s", e)
        finally:
            conn.close()

        return accounts

    def _read_old_user_ids(self) -> dict[str, str]:
        """Read user_ids from the old SQLite DB."""
        user_ids: dict[str, str] = {}
        if not Path(self.old_db_path).exists():
            return user_ids

        conn = sqlite3.connect(self.old_db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM user_ids").fetchall()
            for row in rows:
                d = dict(row)
                user_ids[d["username"]] = d["ig_user_id"]
        except sqlite3.OperationalError as e:
            logger.warning("Error reading old user_ids: %s", e)
        finally:
            conn.close()

        return user_ids

    def _read_json_accounts(self) -> list[dict]:
        """Read accounts from the old JSON export file."""
        if not self.json_path or not Path(self.json_path).exists():
            return []

        with open(self.json_path) as f:
            data = json.load(f)

        # Add user_id from the top-level user_ids dict if available
        user_ids = data.get("user_ids", {})
        accounts = data.get("accounts", [])
        for acct in accounts:
            un = acct.get("username", "")
            if un in user_ids:
                acct["user_id"] = user_ids[un]

        return accounts


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate data from old igautomation schema to new",
    )
    parser.add_argument(
        "--from-db",
        default="output/igautomation.db",
        help="Path to old SQLite database",
    )
    parser.add_argument(
        "--from-json",
        default="output/bd_models.json",
        help="Path to old JSON export",
    )
    parser.add_argument(
        "--to",
        default="igautomation.db",
        help="Path to new database",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without writing",
    )

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    migrator = Migrator(
        old_db_path=args.from_db,
        new_db_path=args.to,
        json_path=args.from_json,
    )

    stats = asyncio.run(migrator.run(dry_run=args.dry_run))

    print("\n=== Migration Results ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
