"""Import accounts from the collector JSON output into the database.

Usage:
    python -m igautomation.import_accounts [output/accounts.json] [--strategy discovery]
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from igautomation.db.store import AsyncDatabaseStore


async def import_accounts(
    json_path: str,
    db_path: str = "igautomation.db",
    strategy: str = "discovery",
) -> None:
    with open(json_path) as f:
        data = json.load(f)

    # Support both flat lists and the collector output format
    if isinstance(data, dict) and "accounts" in data:
        accounts = data["accounts"]
        user_ids = data.get("user_ids", {})
    elif isinstance(data, list):
        accounts = data
        user_ids = {}
    else:
        print(f"Error: unexpected JSON structure in {json_path}")
        sys.exit(1)

    db = AsyncDatabaseStore(db_path)
    await db.initialize()

    imported = 0
    skipped = 0

    for acc in accounts:
        if isinstance(acc, str):
            username = acc
            acc_data = {}
        elif isinstance(acc, dict):
            username = acc.get("username", "")
            acc_data = acc
        else:
            skipped += 1
            continue

        if not username:
            skipped += 1
            continue

        # Map collector output fields to DB fields
        data = {
            "username": username,
            "user_id": acc_data.get("user_id") or user_ids.get(username),
            "full_name": acc_data.get("full_name", ""),
            "bio": acc_data.get("bio", ""),
            "profile_pic_url": acc_data.get("profile_pic_url", ""),
            "is_private": acc_data.get("is_private", False),
            "is_verified": acc_data.get("is_verified", False),
            "follower_count": acc_data.get("follower_count"),
            "following_count": acc_data.get("following_count"),
            "post_count": acc_data.get("post_count"),
            "category": acc_data.get("category"),
            "tier": acc_data.get("tier"),
        }

        try:
            await db.upsert_account(data)
            await db.add_discovery_event(username, strategy=strategy)
            imported += 1
        except Exception as exc:
            print(f"  Error importing @{username}: {exc}")
            skipped += 1

    await db.close()
    print(f"Imported {imported} accounts ({skipped} skipped)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import accounts JSON into DB")
    parser.add_argument("json_path", help="Path to accounts JSON file")
    parser.add_argument("--db", default="igautomation.db", help="Database path")
    parser.add_argument("--strategy", default="discovery", help="Discovery strategy tag")
    args = parser.parse_args()

    asyncio.run(import_accounts(args.json_path, args.db, args.strategy))


if __name__ == "__main__":
    main()
