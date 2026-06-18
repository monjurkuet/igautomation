"""Strategy executors for the daemon — one async function per strategy.

Kept separate from the orchestrator to keep loop.py readable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Callable

from igautomation.daemon.strategies import SessionPlan
from igautomation.db.store import AsyncDatabaseStore

logger = logging.getLogger(__name__)


async def run_blocking(func: Callable, *args: Any, **kwargs: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


# ---------------------------------------------------------------------------
# executors
# ---------------------------------------------------------------------------


async def execute_feed_browsing(
    cdp, graphql, engine, db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    *, rate_limiter: Any = None, current_session_id: str | None = None,
    config: Any = None,
) -> None:
    max_scrolls = plan.params.get("max_scrolls", 15)
    await run_blocking(lambda: cdp.navigate("https://www.instagram.com/", 5))
    await asyncio.sleep(2)
    result = await run_blocking(engine.browse_feed, max_scrolls)
    posts = result.get("posts", [])
    usernames = result.get("usernames", [])
    stats["actions_taken"] = result.get("scrolls_done", 0)
    for post in posts:
        url = post.get("url", "")
        if not url:
            continue
        try:
            ct = "reel" if "/reel/" in url else "post" if "/p/" in url else "unknown"
            await db.upsert_content_item({
                "url": url, "content_type": ct,
                "owner_username": post.get("username", ""),
                "engagement_status": "pending",
            })
        except Exception as e:
            logger.debug("Failed to save feed post %s: %s", url, e)
    new_accounts = 0
    for username in usernames:
        try:
            existing = await db.get_account_by_username(username)
            if not existing:
                account_id = await db.upsert_account({"username": username})
                new_accounts += 1
                await db.add_discovery_event(
                    account_id=account_id, strategy="feed_browsing",
                    source_username=None, query_text="feed_scroll",
                )
        except Exception:
            logger.debug("Failed to upsert account %s", username)
    stats["accounts_discovered"] = new_accounts
    stats["posts_harvested"] = len(posts)
    logger.info("Feed browsing: %d posts, %d usernames (%d new), %d scrolls",
                 len(posts), len(usernames), new_accounts, result.get("scrolls_done", 0))
    await _inline_engagement(cdp, engine, db, posts, stats, current_session_id=current_session_id)


async def execute_reel_browsing(
    cdp, graphql, engine, db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    *, rate_limiter: Any = None, current_session_id: str | None = None,
    config: Any = None,
) -> None:
    max_reels = plan.params.get("max_reels", 20)
    result = await run_blocking(engine.browse_reels, max_reels)
    reels = result.get("reels", [])
    stats["actions_taken"] = result.get("scrolls_done", 0)
    for reel in reels:
        url = reel.get("url", "")
        if not url:
            continue
        try:
            await db.upsert_content_item({
                "url": url, "content_type": "reel",
                "owner_username": reel.get("username", ""),
                "caption": reel.get("caption", ""),
                "engagement_status": "pending",
            })
        except Exception as e:
            logger.debug("Failed to save reel %s: %s", url, e)
    new_accounts = 0
    for reel in reels:
        username = reel.get("username", "")
        if not username:
            continue
        try:
            existing = await db.get_account_by_username(username)
            if not existing:
                account_id = await db.upsert_account({"username": username})
                new_accounts += 1
                await db.add_discovery_event(
                    account_id=account_id, strategy="reel_browsing",
                    source_username=None, query_text="reel_scroll",
                )
        except Exception:
            logger.debug("Failed to upsert reel account %s", username)
    stats["accounts_discovered"] = new_accounts
    stats["reels_harvested"] = len(reels)
    logger.info("Reel browsing: %d reels, %d new accounts, %d swipes",
                 len(reels), new_accounts, result.get("scrolls_done", 0))
    reel_posts = [{"url": r["url"], "username": r.get("username", "")} for r in reels if r.get("url")]
    await _inline_engagement(cdp, engine, db, reel_posts, stats, current_session_id=current_session_id)


async def execute_explore_browsing(
    cdp, graphql, engine, db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    *, rate_limiter: Any = None, current_session_id: str | None = None,
    config: Any = None,
) -> None:
    max_scrolls = plan.params.get("max_scrolls", 10)
    result = await run_blocking(engine.browse_explore, max_scrolls)
    posts = result.get("posts", [])
    usernames = result.get("usernames", [])
    stats["actions_taken"] = result.get("scrolls_done", 0)
    for post in posts:
        url = post.get("url", "")
        if not url:
            continue
        try:
            ct = "reel" if "/reel/" in url else "post" if "/p/" in url else "unknown"
            await db.upsert_content_item({
                "url": url, "content_type": ct,
                "owner_username": post.get("username", ""),
                "engagement_status": "pending",
            })
        except Exception as e:
            logger.debug("Failed to save explore post %s: %s", url, e)
    new_accounts = 0
    for username in usernames:
        try:
            existing = await db.get_account_by_username(username)
            if not existing:
                account_id = await db.upsert_account({"username": username})
                new_accounts += 1
                await db.add_discovery_event(
                    account_id=account_id, strategy="explore_browsing",
                    source_username=None, query_text="explore_scroll",
                )
        except Exception:
            logger.debug("Failed to upsert explore account %s", username)
    stats["accounts_discovered"] = new_accounts
    stats["posts_harvested"] = len(posts)
    logger.info("Explore browsing: %d posts, %d usernames (%d new), %d scrolls",
                 len(posts), len(usernames), new_accounts, result.get("scrolls_done", 0))
    await _inline_engagement(cdp, engine, db, posts, stats, current_session_id=current_session_id)


async def execute_discovery(
    cdp, graphql, engine, db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    *, rate_limiter: Any = None, current_session_id: str | None = None,
    config: Any = None,
) -> None:
    from igautomation.scraper.collector import AccountCollector
    collector = AccountCollector(cdp, graphql, engine)
    target = plan.params.get("target_count", 100)
    strategies = plan.params.get("strategies", ["feed_browse", "discover_people"])
    seeds = plan.params.get("seeds", [])
    accounts = collector.collect(seed_usernames=seeds, target_count=target, strategies=strategies)
    discovered_count = 0
    query_info = json.dumps({"sub_strategies": strategies, "seeds": seeds})
    for username in accounts:
        try:
            existing = await db.get_account_by_username(username)
            is_new = existing is None
            account_id = await db.upsert_account({"username": username})
            await db.add_discovery_event(
                account_id=account_id, strategy=plan.strategy,
                source_username=seeds[0] if seeds else None,
                query_text=query_info,
            )
            if is_new:
                discovered_count += 1
        except Exception as e:
            logger.debug("Failed to save %s: %s", username, e)
    stats["accounts_discovered"] = discovered_count
    stats["actions_taken"] = (
        engine._session.likes_used + engine._session.follows_used +
        engine._session.profile_views_used + engine._session.searches_used
    )


async def execute_profiling(
    cdp, graphql, engine, db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    *, rate_limiter: Any = None, current_session_id: str | None = None,
    config: Any = None,
) -> None:
    from igautomation.scraper.analyzer import ProfileAnalyzer
    batch_size = plan.params.get("batch_size", 20)
    unanalyzed = await db.get_unanalyzed_accounts(limit=batch_size)
    if not unanalyzed:
        logger.info("No accounts needing profiling")
        return
    usernames = [a["username"] for a in unanalyzed]
    logger.info("Profiling %d accounts (chunked)", len(usernames))
    analyzer = ProfileAnalyzer(cdp, graphql, engine)
    chunk_size = 10
    all_profiles = []
    for i in range(0, len(usernames), chunk_size):
        chunk = usernames[i:i + chunk_size]
        try:
            chunk_profiles = await asyncio.wait_for(
                run_blocking(analyzer.analyze, chunk),
                timeout=120,
            )
            all_profiles.extend(chunk_profiles)
        except asyncio.TimeoutError:
            logger.warning("Profiling chunk %d-%d timed out after 120s", i, i + chunk_size)
            continue
        except Exception as exc:
            logger.warning("Profiling chunk %d-%d failed: %s", i, i + chunk_size, exc)
            continue
        # Async commit after each chunk so partial data is saved
        for profile in chunk_profiles:
            try:
                await db.upsert_account({
                    "username": profile.username, "full_name": profile.full_name,
                    "bio": profile.bio, "follower_count": profile.follower_count,
                    "following_count": profile.following_count, "post_count": profile.post_count,
                    "is_private": int(profile.is_private), "is_verified": int(profile.is_verified),
                    "tier": profile.tier, "category": profile.category,
                })
            except Exception as e:
                logger.debug("Failed to save profile %s: %s", profile.username, e)
        # Mark dead accounts in this chunk
        chunk_usernames = {p.username for p in chunk_profiles}
        for username in chunk:
            if username not in chunk_usernames:
                try:
                    await db.upsert_account({
                        "username": username,
                        "follower_count": 0,
                        "tier": "dead",
                    })
                except Exception:
                    logger.debug("Failed to mark dead account %s", username)
    profiles = all_profiles
    if rate_limiter:
        rate_limiter.record_success()
    stats["accounts_profiled"] = len(profiles)
    stats["actions_taken"] = engine._session.profile_views_used
    try:
        growth_counts = await db.refresh_growth_for_all()
        if growth_counts:
            logger.info("Growth status recomputed after profiling: %s", growth_counts)
    except Exception as e:
        logger.debug("Growth recomputation failed after profiling: %s", e)


async def execute_monitoring(
    cdp, graphql, engine, db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    *, rate_limiter: Any = None, current_session_id: str | None = None,
    config: Any = None,
) -> None:
    max_accounts = plan.params.get("max_accounts", 30)
    cur = await db.db.execute(
        """SELECT id, username, follower_count FROM accounts
           WHERE (last_checked_at IS NULL OR last_checked_at < datetime('now', '-1 day'))
           ORDER BY last_checked_at ASC LIMIT ?""",
        (max_accounts,),
    )
    rows = await cur.fetchall()
    if not rows:
        logger.info("No accounts to monitor")
        return
    monitored_count = 0
    for row in rows:
        if engine._session.is_exhausted():
            break
        account_id, username = row["id"], row["username"]
        if rate_limiter:
            await rate_limiter.acquire()
        try:
            profile_data = await run_blocking(graphql.get_web_profile_info, username)
            if rate_limiter:
                rate_limiter.record_success()
        except Exception:
            if rate_limiter:
                rate_limiter.record_error("profile_api")
            continue
        if not profile_data:
            continue
        new_followers = (profile_data.get("edge_followed_by", {}) or {}).get("count", 0)
        new_following = (profile_data.get("edge_follow", {}) or {}).get("count", 0)
        new_posts = (profile_data.get("edge_owner_to_timeline_media", {}) or {}).get("count", 0)
        await db.upsert_account({
            "username": username, "follower_count": new_followers,
            "following_count": new_following, "post_count": new_posts,
        })
        await db.add_follower_snapshot(
            account_id=account_id, follower_count=new_followers,
            following_count=new_following, post_count=new_posts,
        )
        engine._delay()
        engine._session.profile_views_used += 1
        stats["actions_taken"] += 1
        monitored_count += 1
    stats["accounts_monitored"] = monitored_count
    try:
        growth_counts = await db.refresh_growth_for_all()
        logger.info("Growth status recomputed: %s", growth_counts)
    except Exception as e:
        logger.debug("Growth recomputation failed: %s", e)


async def execute_engagement(
    cdp, graphql, engine, db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    *, rate_limiter: Any = None, current_session_id: str | None = None,
    config: Any = None,
) -> None:
    max_follows = plan.params.get("max_follows", 2)
    max_profile_views = plan.params.get("max_profile_views", 5)
    cur = await db.db.execute(
        """SELECT a.id, a.username FROM accounts a
           LEFT JOIN interaction_log il ON a.id = il.account_id
           WHERE il.id IS NULL
           ORDER BY COALESCE(a.follower_count, 0) DESC, a.last_checked_at ASC
           LIMIT 20""",
    )
    rows = await cur.fetchall()
    if not rows:
        logger.info("No accounts for engagement")
        return
    follows_done = 0
    views_done = 0
    for row in rows:
        if engine._session.is_exhausted():
            break
        if follows_done >= max_follows and views_done >= max_profile_views:
            break
        account_id, username = row["id"], row["username"]
        if follows_done < max_follows and engine.can_follow() and random.random() < 0.3:
            await run_blocking(engine.follow_user, username)
            await db.log_interaction(account_id, "follow", username, current_session_id)
            follows_done += 1
            stats["actions_taken"] += 1
        if views_done < max_profile_views and engine.can_view_profile() and random.random() < 0.5:
            await run_blocking(engine.view_profile, username)
            await db.log_interaction(account_id, "view_profile", username, current_session_id)
            views_done += 1
            stats["actions_taken"] += 1
    if not engine._session.is_exhausted():
        await run_blocking(engine.scroll_feed, random.randint(2, 5))
        stats["actions_taken"] += 1


def _content_type_from_db(value: str) -> str:
    v = value.lower().strip() if value else ""
    mapping = {
        "reel": "reel", "reels": "reel", "clip": "reel",
        "carousel": "carousel", "album": "carousel",
        "post": "post", "photo": "post", "image": "post",
        "video": "video",
    }
    return mapping.get(v, "unknown")


async def execute_content_engagement(
    cdp, graphql, engine, db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    *, rate_limiter: Any = None, current_session_id: str | None = None,
    config: Any = None,
) -> None:
    from igautomation.content.engager import ContentEngager
    from igautomation.content.analyzer import analyze_content_browse
    from igautomation.content.models import ContentItem as CI, ContentType, EngagementStatus

    max_items = plan.params.get("max_items", 10)
    do_analyze = plan.params.get("analyze", True)
    cur = await db.db.execute(
        """SELECT id, url, content_type, shortcode FROM content_items
        WHERE engagement_status IN ('pending', 'analyzed')
        ORDER BY RANDOM() LIMIT ?""",
        (max_items,),
    )
    rows = await cur.fetchall()
    if not rows:
        logger.info("No pending content items for engagement")
        return
    engager = ContentEngager(cdp, db)
    for row in rows:
        if engine._session.is_exhausted():
            break
        item_id, url, content_type = row["id"], row["url"], row["content_type"]
        shortcode = row["shortcode"] if "shortcode" in row.keys() else ""
        try:
            mapped = _content_type_from_db(content_type)
            ct = getattr(ContentType, mapped.upper(), ContentType.VIDEO)
            item = CI(url=url, content_type=ct, shortcode=shortcode or "")
            result = engager.engage_content(item)
            stats["actions_taken"] += 1
            await engager.log_engagement(item, result, session_id=current_session_id)
            if result.error:
                overall_status = "error"
            elif result.like == EngagementStatus.DONE or result.save == EngagementStatus.DONE:
                overall_status = "engaged"
            else:
                overall_status = "viewed"
            await db.update_content_engagement_status(item_id, overall_status)
            logger.info("Engaged content %s -> %s", url, overall_status)
        except Exception as e:
            logger.warning("Content engagement failed for %s: %s", url, e)
            await db.update_content_engagement_status(item_id, "error")
            continue
        if do_analyze and not engine._session.is_exhausted():
            try:
                analyzed_item = analyze_content_browse(cdp, item, dwell=3.0)
                if analyzed_item and analyzed_item.llm_analysis:
                    await db.upsert_content_item({
                        "url": url, "llm_analysis": analyzed_item.llm_analysis,
                        "category": analyzed_item.category,
                        "llm_collection_suggestion": analyzed_item.llm_collection_suggestion,
                        "is_bd_relevant": analyzed_item.is_bd_relevant,
                        "content_niche": analyzed_item.content_niche,
                    })
                    stats["actions_taken"] += 1
                    logger.info("Analyzed content %s -- category=%s", shortcode, analyzed_item.category or "?")
                collection_name = (analyzed_item.llm_collection_suggestion or "").strip() if analyzed_item else ""
                if collection_name:
                    try:
                        collection_id = await db.upsert_collection(name=collection_name)
                        await db.add_content_to_collection(item_id, collection_id)
                        logger.info("Linked content %s -> collection '%s'", shortcode, collection_name)
                    except Exception as ce:
                        logger.debug("Collection link failed for %s: %s", shortcode, ce)
            except Exception as e:
                logger.warning("Content analysis failed for %s: %s", url, e)
    if not engine._session.is_exhausted():
        await run_blocking(engine.scroll_feed, random.randint(1, 3))
        stats["actions_taken"] += 1


# ---------------------------------------------------------------------------
# new / missing strategy handlers
# ---------------------------------------------------------------------------


async def execute_story_viewing(
    cdp, graphql, engine, db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    *, rate_limiter: Any = None, current_session_id: str | None = None,
    config: Any = None,
) -> None:
    stats["skipped_reason"] = "not_implemented"
    logger.warning("Story viewing not yet implemented -- skipping")


async def execute_auto_unfollow(
    cdp, graphql, engine, db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    *, rate_limiter: Any = None, current_session_id: str | None = None,
    config: Any = None,
) -> None:
    stats["skipped_reason"] = "not_implemented"
    logger.warning("Auto unfollow not yet implemented -- skipping")


async def execute_comment_engagement(
    cdp, graphql, engine, db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    *, rate_limiter: Any = None, current_session_id: str | None = None,
    config: Any = None,
) -> None:
    comment_enabled = (config or {}).get("comment_enabled", False) if isinstance(config, dict) else getattr(config, "comment_enabled", False)
    if comment_enabled:
        logger.info("Comment engagement implementation pending")
    else:
        stats["skipped_reason"] = "comment_engagement_disabled"
        logger.info("Comment engagement is disabled by default -- skipping")


async def execute_own_account_monitoring(
    cdp, graphql, engine, db: AsyncDatabaseStore, plan: SessionPlan, stats: dict,
    *, rate_limiter: Any = None, current_session_id: str | None = None,
    config: Any = None,
) -> None:
    accounts = await db.get_available_ig_accounts()
    if not accounts:
        logger.info("No own IG accounts registered")
        return
    for acct in accounts:
        if engine._session.is_exhausted():
            break
        try:
            profile_data = await run_blocking(graphql.get_web_profile_info, acct["username"])
            if profile_data:
                fc = (profile_data.get("edge_followed_by", {}) or {}).get("count", 0)
                fol = (profile_data.get("edge_follow", {}) or {}).get("count", 0)
                await db.snapshot_own_account(acct["id"], fc, following_count=fol)
                stats["actions_taken"] += 1
        except Exception as e:
            logger.debug("Own account monitoring failed for %s: %s", acct["username"], e)
    logger.info("Own account monitoring: refreshed %d accounts", len(accounts))


# ---------------------------------------------------------------------------
# inline engagement helper
# ---------------------------------------------------------------------------


async def _inline_engagement(
    cdp, engine, db: AsyncDatabaseStore, posts: list[dict], stats: dict,
    *, current_session_id: str | None = None,
) -> None:
    max_inline_likes = 5
    max_inline_follows = 2
    likes_done = 0
    follows_done = 0
    for post in posts:
        if likes_done >= max_inline_likes and follows_done >= max_inline_follows:
            break
        if engine._session.is_exhausted():
            break
        url = post.get("url", "")
        username = post.get("username", "")
        if likes_done < max_inline_likes and engine.can_like() and random.random() < 0.2:
            try:
                liked = await run_blocking(engine.like_post, url)
                if liked:
                    likes_done += 1
                    stats["actions_taken"] += 1
                    stats.setdefault("likes_done", 0)
                    stats["likes_done"] += 1
                    # Log interaction to DB
                    try:
                        if username:
                            account = await db.get_account_by_username(username)
                            if account:
                                await db.log_interaction(account["id"], "like", url, current_session_id)
                        await db.update_content_engagement_status_by_url(url, "engaged")
                    except Exception:
                        logger.debug("Failed to log like interaction for %s", url)
            except Exception:
                logger.debug("Failed to like post %s", url)
        if follows_done < max_inline_follows and username and engine.can_follow() and random.random() < 0.1:
            try:
                followed = await run_blocking(engine.follow_user, username)
                if followed:
                    follows_done += 1
                    stats["actions_taken"] += 1
                    stats.setdefault("follows_done", 0)
                    stats["follows_done"] += 1
                    account = await db.get_account_by_username(username)
                    if account:
                        await db.log_interaction(account["id"], "follow", username, current_session_id)
            except Exception as e:
                logger.debug("Failed to follow user %s inline: %s", username, e)


# ---------------------------------------------------------------------------
# strategy registry
# ---------------------------------------------------------------------------

def build_strategy_registry() -> dict[str, Callable]:
    return {
        "feed_browsing": execute_feed_browsing,
        "reel_browsing": execute_reel_browsing,
        "explore_browsing": execute_explore_browsing,
        "discovery": execute_discovery,
        "profiling": execute_profiling,
        "monitoring": execute_monitoring,
        "engagement": execute_engagement,
        "content_engagement": execute_content_engagement,
        "story_viewing": execute_story_viewing,
        "auto_unfollow": execute_auto_unfollow,
        "comment_engagement": execute_comment_engagement,
        "own_account_monitoring": execute_own_account_monitoring,
    }


def strategy_registry_covers_fallback_plans() -> set[str]:
    from igautomation.daemon.strategies import FALLBACK_PLANS
    registry = build_strategy_registry()
    missing = {p.strategy for p in FALLBACK_PLANS} - set(registry)
    return missing
