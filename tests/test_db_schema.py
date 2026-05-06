"""Tests for the async database store."""
import pytest_asyncio

from igautomation.db.store import AsyncDatabaseStore


@pytest_asyncio.fixture
async def store():
    """Create an in-memory store, initialize, and tear down."""
    s = AsyncDatabaseStore(":memory:")
    await s.initialize()
    yield s
    await s.close()


async def test_upsert_account_insert(store):
    aid = await store.upsert_account({"username": "testuser", "follower_count": 1000})
    assert aid == 1
    acc = await store.get_account_by_username("testuser")
    assert acc is not None
    assert acc["username"] == "testuser"
    assert acc["follower_count"] == 1000


async def test_upsert_account_update(store):
    await store.upsert_account({"username": "testuser", "follower_count": 1000})
    aid2 = await store.upsert_account({"username": "testuser", "follower_count": 1500, "tier": "micro"})
    assert aid2 == 1
    acc = await store.get_account_by_username("testuser")
    assert acc["follower_count"] == 1500
    assert acc["tier"] == "micro"


async def test_get_account_by_username_not_found(store):
    acc = await store.get_account_by_username("nonexistent")
    assert acc is None


async def test_add_discovery_event(store):
    aid = await store.upsert_account({"username": "discovered1"})
    eid = await store.add_discovery_event(aid, "suggestion", source_username="seed_user")
    assert eid >= 1


async def test_get_discovery_stats(store):
    a1 = await store.upsert_account({"username": "u1"})
    a2 = await store.upsert_account({"username": "u2"})
    await store.add_discovery_event(a1, "suggestion", source_username="x")
    await store.add_discovery_event(a2, "search", query_text="models")
    await store.add_discovery_event(a2, "suggestion", source_username="y")
    stats = await store.get_discovery_stats()
    assert stats["suggestion"] == 2
    assert stats["search"] == 1


async def test_log_interaction(store):
    aid = await store.upsert_account({"username": "interacted"})
    iid = await store.log_interaction(aid, "view_profile", detail="https://instagram.com/interacted/")
    assert iid >= 1


async def test_add_follower_snapshot(store):
    aid = await store.upsert_account({"username": "grower", "follower_count": 500})
    sid = await store.add_follower_snapshot(aid, 600, following_count=200, post_count=50)
    assert sid >= 1


async def test_session_lifecycle(store):
    sid = await store.create_session("test-uuid-123")
    assert sid >= 1
    await store.end_session("test-uuid-123", actions_taken=42, accounts_discovered=10, status="completed")


async def test_add_analysis(store):
    aid = await store.upsert_account({"username": "analyzed"})
    alid = await store.add_analysis(aid, "relevance", result="high", model_used="gpt-4")
    assert alid >= 1


async def test_get_accounts_by_tier(store):
    await store.upsert_account({"username": "mega1", "tier": "mega", "relevance_score": 0.9})
    await store.upsert_account({"username": "micro1", "tier": "micro", "relevance_score": 0.5})
    await store.upsert_account({"username": "mega2", "tier": "mega", "relevance_score": 0.95})
    megas = await store.get_accounts_by_tier("mega")
    assert len(megas) == 2
    assert megas[0]["username"] == "mega2"  # higher score first


async def test_get_unanalyzed_accounts(store):
    await store.upsert_account({"username": "no_analysis", "relevance_score": 0.8})
    a2 = await store.upsert_account({"username": "has_analysis", "relevance_score": 0.9})
    await store.add_analysis(a2, "relevance", result="high")
    unanalyzed = await store.get_unanalyzed_accounts(limit=10)
    assert len(unanalyzed) == 1
    assert unanalyzed[0]["username"] == "no_analysis"
