"""Load and outage fixes from the eval round of items 35-46, each test one finding."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from shopping_agent import ShoppingSessionContext

from my_store import chips, discovery
from my_store.shopify_backend import ShopifyUCPBackend
from my_store.textflow import TextFlow

from .fake_shopify import SHOP, FakeShopifyStore
from .test_shopify_backend import make_backend, make_executor


@pytest.fixture(autouse=True)
def instant_sleep(monkeypatch):
    real = asyncio.sleep

    async def instant(seconds, *args):
        await real(0)

    monkeypatch.setattr("my_store.shopify_backend.asyncio.sleep", instant)


@pytest.fixture(autouse=True)
def keep_globals():
    vocabulary, starters = set(discovery.VOCABULARY), list(discovery.STARTER_CHIPS)
    yield
    discovery.set_vocabulary(vocabulary)
    discovery.STARTER_CHIPS[:] = starters


@pytest.fixture
def store() -> FakeShopifyStore:
    return FakeShopifyStore()


def ctx() -> ShoppingSessionContext:
    return ShoppingSessionContext(session_id="s1", user_id="u")


def backend_over(handler) -> ShopifyUCPBackend:
    return ShopifyUCPBackend(
        SHOP,
        agent_profile_url="https://agent.example/ucp-profile.json",
        buyer_country="US",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


# 35: at most 4 Shopify calls in flight, however many chats search at once.
async def test_shopify_calls_are_capped_in_flight(store):
    state = {"now": 0, "peak": 0}

    async def slow(request):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return store._handle(request)

    backend = backend_over(slow)
    await asyncio.gather(*(backend.search_products(ctx(), f"tour {i}") for i in range(20)))
    assert state["peak"] <= 4


# 35: a country's cities come from the index, not a dozen more Shopify searches.
async def test_expansions_use_the_index_not_more_calls(store):
    backend = make_backend(store)
    await backend.catalog_index()
    before = sum(1 for name, _, _ in store.calls if name == "search_catalog")
    found = await backend.search_products(ctx(), "malaysia")
    after = sum(1 for name, _, _ in store.calls if name == "search_catalog")
    assert [p.title for p in found] == ["Kuala Lumpur City Tour"]
    assert after - before == 1


# 35, 46: a timeout or dropped connection is tried once more before it counts as a failure.
async def test_a_dropped_connection_is_retried_once(store):
    drops = {"left": 1}

    def flaky(request):
        if request.url.path == "/api/ucp/mcp" and drops["left"]:
            drops["left"] -= 1
            raise httpx.ConnectError("connection reset")
        return store._handle(request)

    backend = backend_over(flaky)
    assert await backend.search_products(ctx(), "tent")


# 43: a throttle pauses catalog calls for seconds, growing only while it repeats.
async def test_the_pause_after_a_throttle_is_short_and_resets(store):
    backend = make_backend(store)
    store.search_failures = 3  # one search: first try and two retries
    with pytest.raises(Exception, match="could not be checked"):
        await backend.search_products(ctx(), "tent")
    first = backend.health()["catalog_paused_seconds"]
    assert 0 < first <= 10
    backend._cooldown_until = 0.0
    store.search_failures = 3
    with pytest.raises(Exception, match="could not be checked"):
        await backend.search_products(ctx(), "tent")
    assert 10 < backend.health()["catalog_paused_seconds"] <= 20
    backend._cooldown_until = 0.0
    assert await backend.search_products(ctx(), "tent")
    assert backend._throttles == 0


# 36, 40: a search that could not run tells the model not to claim absence, in shopper words;
# one answered from the index says so.
async def test_search_failure_and_index_answers_are_flagged(store):
    backend = make_backend(store)
    ex = make_executor(backend)
    store.search_failures = 100
    failed = await ex.execute("search_products", {"query": "tent"})
    assert "couldn't check the catalog just now" in failed.result_text
    assert "Do not say the store does not carry" in failed.result_text
    assert "tool" not in failed.result_text.split("'")[1]  # the line for the shopper

    store.search_failures = 0
    backend._cooldown_until = 0.0
    await backend.catalog_index()
    backend._cooldown_until = 1e12  # Shopify throttling: answered from the index
    from_index = await ex.execute("search_products", {"query": "tent"})
    assert not from_index.is_error and "catalog list" in from_index.result_text


# 42: products added after the index was read join it from live search; the index can be
# re-read on demand, and /healthz says how old it is.
async def test_new_products_join_the_index(store):
    from .fake_shopify import _COLLECTIONS, _PRODUCTS, _VARIANTS

    backend = make_backend(store)
    await backend.catalog_index()
    assert backend.health()["catalog_synced_seconds_ago"] == 0
    pid = "gid://shopify/Product/9"
    _PRODUCTS[pid] = {"title": "Singapore Hotel Voucher", "description": {"html": "2 nights."}, "options": {}}
    _VARIANTS["gid://shopify/ProductVariant/901"] = (pid, {"Title": "Default Title"}, 30000, True)
    _COLLECTIONS[pid] = ["Travel"]
    try:
        version = backend.index_version
        await backend.search_products(ctx(), "hotel")
        assert any(p.title == "Singapore Hotel Voucher" for p in backend._index)
        assert backend.index_version > version
        assert len(await backend.refresh_index()) == 6
    finally:
        del _PRODUCTS[pid], _VARIANTS["gid://shopify/ProductVariant/901"], _COLLECTIONS[pid]


def test_catalog_refresh_endpoint_needs_its_token(monkeypatch):
    from fastapi.testclient import TestClient

    from my_store import app as host

    client = TestClient(host.app)
    assert client.post("/api/catalog/refresh").status_code == 404
    monkeypatch.setattr(host, "CATALOG_REFRESH_TOKEN", "r3fresh")
    assert client.post("/api/catalog/refresh", headers={"x-refresh-token": "wrong"}).status_code == 404
    assert client.post("/api/catalog/refresh", headers={"x-refresh-token": "r3fresh"}).status_code == 200


# 37: a 429 says when to retry, in the header and the JSON.
def test_a_429_says_when_to_retry(monkeypatch):
    from fastapi.testclient import TestClient

    from my_store import app as host
    from my_store import guards

    monkeypatch.setattr(guards, "session_limiter", guards.RateLimiter(1, 3600))
    client = TestClient(host.app)
    client.post("/api/session", json={})
    refused = client.post("/api/session", json={})
    assert refused.status_code == 429
    wait = int(refused.headers["Retry-After"])
    assert 3500 < wait <= 3601 and refused.json()["retry_after"] == wait
    assert "Too many new chats" in refused.json()["detail"]


# 38: two text blocks with no tool call between them do not run together.
def test_text_blocks_get_a_paragraph_break():
    flow = TextFlow()
    out = (
        flow.feed("I can't check right now.")
        + flow.feed("No smartwatches here.")
        + flow.feed(" Version 2.5.")
    )
    assert out == "I can't check right now.\n\nNo smartwatches here. Version 2.5."


# 45: after a failed call, no chip that just retries it.
def test_no_retry_chips_after_an_error():
    kept = chips.clean(
        ["Try the search again", "Retry gift search", "Show more tours"],
        vocabulary=set(),
        cart_titles=[],
        after_error=True,
    )
    assert kept == ["Show more tours"]
