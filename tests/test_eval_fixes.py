"""Fixes for the search, cart, and chip-crawl evals (items 20-34), each test one finding."""

from __future__ import annotations

import httpx
import pytest
from shopping_agent import SearchFilters, ShoppingSessionState

from my_store import chips, discovery
from my_store.executor import note_turn
from my_store.textflow import TextFlow

from .fake_shopify import FakeShopifyStore
from .test_shopify_backend import TEE, TEE_M, make_backend, make_executor


@pytest.fixture(autouse=True)
def no_retry_waits(monkeypatch):
    async def instant(_):
        return None

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


def ctx():
    from my_store.app import ShoppingSessionContext

    return ShoppingSessionContext(session_id="s1", user_id="u")


# 21: a price ranking covers the whole catalog, read page by page.
async def test_most_expensive_ranks_the_whole_catalog(store, monkeypatch):
    monkeypatch.setattr("my_store.shopify_backend._INDEX_PAGE", 2)  # force several pages
    backend = make_backend(store)
    top = await backend.search_products(ctx(), "what is your most expensive item")
    assert top[0].title == "Summit 2P Backpacking Tent"  # $229, not in a relevance cut
    cheap = await backend.search_products(ctx(), "cheapest tour")
    assert [p.title for p in cheap] == ["Kuala Lumpur City Tour", "Canyon Day Tour"]
    by_sort = await backend.search_products(ctx(), "", SearchFilters(sort="price_asc"), 1)
    assert by_sort[0].title == "Trail Socks"
    assert len(await backend.catalog_index()) == 5


# 25, 33: collections come from the catalog, most specific first.
async def test_collections_from_the_catalog(store):
    backend = make_backend(store)
    await backend.catalog_index()
    groups = backend.collections()
    assert set(groups) == {"Clothing", "Camping", "Tours"}
    assert {"merino", "tent", "tour", "clothing"} <= backend.vocabulary()
    discovery.set_starters(list(groups))
    assert discovery.STARTER_CHIPS[0] in ("Shop Clothing", "Shop Tours")


# 31: search failing (rate limited) answers from the index and the store reads as degraded.
async def test_search_outage_answers_from_the_index(store):
    backend = make_backend(store)
    await backend.catalog_index()
    store.search_failures = 100
    found = await backend.search_products(ctx(), "tour")
    assert {p.title for p in found} == {"Canyon Day Tour", "Kuala Lumpur City Tour"}
    for _ in range(3):
        await backend.search_products(ctx(), "tent")
    assert backend.degraded() and backend.health()["failures_last_5m"] >= 3
    store.search_failures = 0
    await backend.search_products(ctx(), "tent")
    assert not backend.degraded()


async def test_search_outage_with_no_index_says_what_happened(store):
    backend = make_backend(store)
    store.search_failures = 100
    ex = make_executor(backend)
    out = await ex.execute("search_products", {"query": "tent"})
    assert out.is_error and "not answering right now" in out.result_text


# 26: the store's country unknown (meta.json rate limited) no longer refuses shipped items.
async def test_cart_refusal_rereads_the_store_country_and_retries(store):
    store.meta_failures = 1
    backend = make_backend(store, buyer_country="GB")  # a country the store doesn't ship to
    ex = make_executor(backend)
    await ex.execute("search_products", {"query": "tee"})
    await ex.execute("get_product_details", {"product_id": TEE})
    out = await ex.execute("add_to_cart", {"product_id": "gid://shopify/ProductVariant/101"})
    assert not out.is_error, out.result_text
    assert backend.health()["buyer_country"] == "US"


# 27: no tool error reaches the model empty.
async def test_tool_errors_are_never_empty(store):
    ex = make_executor(make_backend(store))
    timeout = ex.domain_error(httpx.ReadTimeout(""))
    assert "did not answer in time" in timeout.result_text
    odd = ex.domain_error(RuntimeError(""))
    assert odd.is_error and "RuntimeError" in odd.result_text


# 30: an add that fits two products asks first, once.
async def test_an_ambiguous_add_asks_which(store):
    ex = make_executor(make_backend(store))
    note_turn(ex._state, "add the tour")
    await ex.execute("search_products", {"query": "tour"})
    tour = "gid://shopify/Product/4"
    first = await ex.execute("add_to_cart", {"product_id": tour})
    assert (
        first.is_error
        and "Canyon Day Tour" in first.result_text
        and "Kuala Lumpur City Tour" in first.result_text
    )
    note_turn(ex._state, "add the Canyon tour")
    await ex.execute("search_products", {"query": "tour"})
    second = await ex.execute("add_to_cart", {"product_id": tour})
    assert not second.is_error, second.result_text


async def test_a_size_choice_is_not_ambiguous(store):
    ex = make_executor(make_backend(store))
    note_turn(ex._state, "add the merino tee in M")
    await ex.execute("search_products", {"query": "tee"})
    await ex.execute("get_product_details", {"product_id": TEE})
    out = await ex.execute("add_to_cart", {"product_id": TEE_M})
    assert "fits more than one" not in out.result_text


# 20: a word naming something the store sells searches; synonyms find it.
async def test_hotel_searches_and_finds_stays(monkeypatch):
    discovery.set_vocabulary({"tour", "tent"})
    assert discovery.shopping_request("hotels") and discovery.shopping_request("tent?")
    assert not discovery.shopping_request("hello there")
    from my_store.shopify_backend import _synonym_queries

    assert set(_synonym_queries("hotel in bangkok")) >= {"villa", "stay"}


# 24: "what do you sell" is answered from collections, not forced searches.
def test_overview_question_is_not_forced_to_search():
    state = ShoppingSessionState()
    assert discovery.OVERVIEW.search("what do you sell?")
    assert discovery._discovery(None, "what do you sell?", state) is None


# 28: a cart change reads the cart first.
def test_cart_change_reads_the_cart_first():
    for text in ("change mug to 3", "make it 2 mugs", "remove the eSIM", "I only want 1"):
        assert discovery.CART_EDIT_RULE.fires(None, text, ShoppingSessionState()) == {}, text
    assert discovery.CART_EDIT_RULE.fires(None, "show me mugs", ShoppingSessionState()) is None


# 29, 32, 34: chips the store can honour.
def test_chips_are_checked():
    vocab = {"mug", "bookworm", "tour", "tokyo", "esim", "japan"}
    kept = chips.clean(
        [
            "Notify me when back in stock",
            "Add Bookworm mug",  # already in the cart
            "Add Japan eSIM",
            "Compare the drone and the mug",  # no drone
            "Compare Tokyo tour and Japan eSIM",
            "Remove the tent",  # not in the cart
            "Show more Tokyo tours",
            "Shop electronics",  # the store sells none
        ],
        vocabulary=vocab,
        cart_titles=["Bookworm Ceramic Mug"],
    )
    assert kept == ["Add Japan eSIM", "Compare Tokyo tour and Japan eSIM", "Show more Tokyo tours"]
    assert chips.clean(["Check out", "Show more tours"], vocabulary=vocab, cart_titles=[]) == [
        "Show more tours"
    ]
    degraded = chips.clean(
        ["Try again", "Show more tours", "Check out"], vocabulary=vocab, cart_titles=["Mug"], degraded=True
    )
    assert degraded == ["Check out"]


# 22: text before and after a tool call: a paragraph break, and no repeat.
def test_reply_text_is_not_glued_or_repeated():
    def run(parts):
        flow, out = TextFlow(), ""
        for i, part in enumerate(parts):
            if i:
                flow.tool()
            for j in range(0, len(part), 5):
                out += flow.feed(part[j : j + 5])
        return out + flow.end()

    first = "Let me check our catalog for you."
    assert run([first, first + " Here are three hotels."]) == first + "\n\nHere are three hotels."
    assert run(["Searching.", "Found two."]) == "Searching.\n\nFound two."
    assert run([first, first]) == first


# 23: an empty or blank message gets a friendly prompt.
def test_blank_messages_get_a_prompt():
    from fastapi.testclient import TestClient

    from my_store import app as host

    client = TestClient(host.app)
    sid = client.post("/api/session", json={}).json()["session_id"]
    for body in ({"message": ""}, {"message": "   "}, {}):
        r = client.post("/api/chat", headers={"x-session-id": sid}, json=body)
        assert r.status_code == 200 and "What can I help you find?" in r.text


# 31: a load-test token skips the per-IP limits; /healthz shows the store's health.
def test_load_test_token_skips_per_ip_limits(monkeypatch):
    from fastapi.testclient import TestClient

    from my_store import app as host
    from my_store import guards

    monkeypatch.setattr(host, "LOAD_TEST_TOKEN", "t0ken")
    monkeypatch.setattr(guards, "session_limiter", guards.RateLimiter(1, 3600))
    client = TestClient(host.app)
    client.post("/api/session", json={})
    assert client.post("/api/session", json={}).status_code == 429
    ok = client.post("/api/session", json={}, headers={"x-load-test-token": "t0ken"})
    assert ok.status_code == 200
    assert "commit" in client.get("/healthz").json()
