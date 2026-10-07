"""The wrap-up list: get_cart never fails empty (49), "this" is the page's product (16),
chip prices in the store's currency, long searches that find nothing, /healthz commit."""

from __future__ import annotations

import pytest
from commerce_common.presentation import EnrichmentContext, PresentationRefused
from shopping_agent import Product, ShoppingSessionContext, ShoppingSessionState

from my_store import compare
from my_store.chips import in_currency

from .fake_shopify import FakeShopifyStore
from .test_shopify_backend import make_backend


def ctx() -> ShoppingSessionContext:
    return ShoppingSessionContext(session_id="s1", user_id="u")


async def test_get_cart_answers_with_the_last_written_cart_when_shopify_fails(monkeypatch):
    store = FakeShopifyStore()
    backend = make_backend(store)
    await backend.search_products(ctx(), "tour")
    await backend.add_to_cart(ctx(), "gid://shopify/Product/4", 2)
    backend._written["s1"] = (0.0, backend._written["s1"][1])  # no longer fresh: read Shopify

    async def down(*args, **kwargs):
        from my_store.shopify_backend import ShopifyError

        raise ShopifyError("get_cart: unavailable")

    monkeypatch.setattr(backend, "_call", down)
    cart = await backend.get_cart(ctx())
    assert [(i.title, i.quantity) for i in cart.items] == [("Canyon Day Tour", 2)]


async def test_get_cart_with_nothing_written_says_what_failed(monkeypatch):
    backend = make_backend(FakeShopifyStore())
    backend._cart_ids["s1"] = "gid://shopify/Cart/x"

    async def down(*args, **kwargs):
        from my_store.shopify_backend import ShopifyError

        raise ShopifyError("")

    monkeypatch.setattr(backend, "_call", down)
    with pytest.raises(Exception, match="cart could not be read"):
        await backend.get_cart(ctx())


async def test_this_on_a_product_page_is_the_pages_product():
    page = Product(product_id="FUJI", title="Mt Fuji Day Trip", price=110)
    old = Product(product_id="KL", title="KL City Tour", price=45)
    mug = Product(product_id="MUG", title="Bookworm Mug", price=16)
    state = ShoppingSessionState()
    state.remember_products([page, old, mug])
    context = EnrichmentContext(backend=None, config=None, session=None, state=state)
    compare.note_viewing(state, "compare this with the mug", page)
    wrong = compare.Payload.model_validate({"entries": [{"product_id": "KL"}, {"product_id": "MUG"}]})
    with pytest.raises(PresentationRefused, match="Mt Fuji Day Trip"):
        await compare.enrich(wrong, context)
    right = compare.Payload.model_validate({"entries": [{"product_id": "FUJI"}, {"product_id": "MUG"}]})
    assert len((await compare.enrich(right, context))["entries"]) == 2
    compare.note_viewing(state, "compare these two", page)  # no "this": history decides
    assert len((await compare.enrich(wrong, context))["entries"]) == 2


def test_chip_prices_are_in_the_stores_currency():
    assert in_currency("Gifts under $50", "SGD") == "Gifts under SGD 50"
    assert in_currency("Gifts under $50", "USD") == "Gifts under $50"


async def test_a_long_query_that_finds_nothing_uses_the_index():
    backend = make_backend(FakeShopifyStore())
    await backend.catalog_index()
    found = await backend.search_products(ctx(), "guided canyon walking hike tour")
    assert found and found[0].title == "Canyon Day Tour"


def test_healthz_names_the_commit():
    from fastapi.testclient import TestClient

    from my_store import app as host

    commit = TestClient(host.app).get("/healthz").json()["commit"]
    assert commit and len(commit) == 7


async def test_the_exact_title_match_comes_first_even_when_shopify_ranks_it_out():
    backend = make_backend(FakeShopifyStore())
    await backend.catalog_index()
    from shopping_agent import SearchFilters

    others = [p for p in backend._index if "Kuala" not in p.title]
    ranked = backend._exact_first("Kuala Lumpur tour", SearchFilters(), others)
    assert ranked[0].title == "Kuala Lumpur City Tour"
    assert backend._exact_first("tour", SearchFilters(), others) == others  # one word: Shopify's order


def test_search_results_fit_the_runtimes_tool_result_limit():
    from shopping_agent import Product

    from my_store.shopify_backend import _fit

    big = [Product(product_id=f"P{i}", title="x" * 400, price=1) for i in range(40)]
    kept = _fit(big)
    assert 0 < len(kept) < 40
    assert sum(len(p.model_dump_json(exclude_none=True)) for p in kept) <= 10_500
