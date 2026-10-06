"""ShopifyUCPBackend against the fake store, driven through the reference tool executor so
the agent's gates (provenance, options) run exactly as they do in a live turn."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from commerce_common.memory import InMemoryMemoryStore
from commerce_common.skills import SkillRegistry
from shopping_agent import SearchFilters, ShoppingSessionContext, ShoppingSessionState
from shopping_agent.executor import build_memory
from shopping_agent.gates import OPTIONS_GATE, PROVENANCE_GATE

from my_store.executor import StoreToolExecutor
from my_store.shopify_backend import SHOPIFY_PROMPT_NOTES, ShopifyUCPBackend, _text, shopify_agent_config

from .fake_shopify import SHOP, FakeShopifyStore

TEE, TEE_S, TEE_M, TEE_L = (
    "gid://shopify/Product/1",
    "gid://shopify/ProductVariant/101",
    "gid://shopify/ProductVariant/102",
    "gid://shopify/ProductVariant/103",
)
TENT = "gid://shopify/Product/2"
TOUR = "gid://shopify/Product/4"


@pytest.fixture
def store() -> FakeShopifyStore:
    return FakeShopifyStore()


def make_backend(store: FakeShopifyStore, **kwargs) -> ShopifyUCPBackend:
    kwargs.setdefault("buyer_country", "US")
    return ShopifyUCPBackend(
        SHOP,
        agent_profile_url="https://agent.example/ucp-profile.json",
        utm_source="claude_agent",
        http=httpx.AsyncClient(transport=store.transport()),
        **kwargs,
    )


def make_executor(backend: ShopifyUCPBackend) -> StoreToolExecutor:
    config = shopify_agent_config(brand_name="Trailhead")
    return StoreToolExecutor(  # the app's executor (my_store/executor.py)
        backend=backend,
        config=config,
        skills=SkillRegistry([]),
        session=ShoppingSessionContext(session_id="s1", user_id="guest-1", now=datetime(2026, 10, 6, 9)),
        state=ShoppingSessionState(),
        memory=build_memory(config, InMemoryMemoryStore()),
    )


async def test_discovery_to_checkout_through_the_executor(store):
    backend = make_backend(store, client_id="id", client_secret="secret")
    backend.set_buyer_ip("s1", "203.0.113.7")
    ex = make_executor(backend)

    # Nothing seen yet: the provenance gate holds the write before Shopify is called.
    held = await ex.execute("add_to_cart", {"product_id": TENT})
    assert held.blocked == PROVENANCE_GATE
    assert not store.calls

    # Discovery: prices arrive in minor units and become dollars; the tent is a plain product.
    found = await ex.execute("search_products", {"query": "merino tee tent", "filters": {"max_price": 250}})
    assert "Ridgeline Merino Tee" in found.result_text and '"price": 48.0' in found.result_text
    assert '"options": {"Size": ["S", "M", "L"]}' in found.result_text
    assert '"Default Title"' not in found.result_text
    # Each product links to its storefront page by handle (live gives a handle, no url).
    assert f"https://{SHOP}/products/ridgeline-merino-tee?utm_source=claude_agent" in found.result_text

    # The family can't be added; details fill in every size, not just the one Shopify returned.
    assert (await ex.execute("add_to_cart", {"product_id": TEE})).blocked == OPTIONS_GATE
    details = await ex.execute("get_product_details", {"product_id": TEE})
    for vid in (TEE_S, TEE_M, TEE_L):
        assert vid in details.result_text
    assert "<b>" not in details.result_text

    # Out of stock is refused with the in-stock sizes named; nothing is written.
    oos = await ex.execute("add_to_cart", {"product_id": TEE_M})
    assert oos.is_error and TEE_S in oos.result_text and TEE_L in oos.result_text
    assert not store.carts

    # Adds: a variant, then a single-variant product resolved to its variant id.
    assert not (await ex.execute("add_to_cart", {"product_id": TEE_L, "quantity": 2})).is_error
    added = await ex.execute("add_to_cart", {"product_id": TENT})
    assert "subtotal 333.00 USD" in added.result_text
    cart = next(iter(store.carts.values()))
    assert {li["item"]["id"]: li["quantity"] for li in cart["line_items"]} == {
        TEE_L: 2,
        "gid://shopify/ProductVariant/201": 1,
    }

    # update_cart is a full replace: changing one line keeps the other.
    await ex.execute("update_cart_item", {"product_id": TEE_L, "quantity": 1})
    assert len(next(iter(store.carts.values()))["line_items"]) == 2

    # Checkout: an authenticated create_checkout; its continue_url goes on the card only.
    out = await ex.execute("checkout", {})
    card = next(e for e in out.events if e.type == "ui").data["payload"]
    url = card["handoffs"][0]["url"]
    assert url.startswith(f"https://{SHOP}/checkouts/cn/") and url.endswith("?utm_source=claude_agent")
    assert url not in out.result_text
    checkout_call = next(c for c in store.calls if c[0] == "create_checkout")
    assert checkout_call[2]["authorization"] == "Bearer header.payload.sig"
    assert checkout_call[2]["shopify-storefront-buyer-ip"] == "203.0.113.7"
    assert len(checkout_call[1]["checkout"]["line_items"]) == 2
    assert checkout_call[1]["checkout"]["context"] == {"address_country": "US"}

    # Every UCP call carried the agent profile.
    assert all(
        c[1]["meta"]["ucp-agent"]["profile"] == "https://agent.example/ucp-profile.json"
        for c in store.calls
        if c[0] != "search_shop_policies_and_faqs"
    )


async def test_checkout_without_credentials_uses_the_cart_link(store):
    backend = make_backend(store)
    ex = make_executor(backend)
    await ex.execute("search_products", {"query": "tent"})
    await ex.execute("add_to_cart", {"product_id": TENT})
    out = await ex.execute("checkout", {})
    url = next(e for e in out.events if e.type == "ui").data["payload"]["handoffs"][0]["url"]
    assert url.startswith(f"https://{SHOP}/cart/c/")
    assert not any(c[0] == "create_checkout" for c in store.calls)


async def test_a_line_the_cart_drops_is_reported_not_added(store):
    ex = make_executor(make_backend(store))
    await ex.execute("search_products", {"query": "socks"})
    out = await ex.execute("add_to_cart", {"product_id": "gid://shopify/Product/3"})
    assert out.is_error and "already sold out" in out.result_text


async def test_checkout_without_a_buyer_ip_falls_back_to_the_cart_link(store):
    ex = make_executor(make_backend(store, client_id="id", client_secret="secret"))
    await ex.execute("search_products", {"query": "tent"})
    await ex.execute("add_to_cart", {"product_id": TENT})
    out = await ex.execute("checkout", {})
    url = next(e for e in out.events if e.type == "ui").data["payload"]["handoffs"][0]["url"]
    assert url.startswith(f"https://{SHOP}/cart/c/")
    assert not any(c[0] == "create_checkout" for c in store.calls)


async def test_a_product_added_by_its_product_id_can_be_changed_and_removed_by_it(store):
    # Live: the agent added the eSIM by product id and later removed it by product id; the
    # cart line is the variant, so nothing matched and the agent reported a removal anyway.
    ex = make_executor(make_backend(store))
    await ex.execute("search_products", {"query": "tent merino"})
    await ex.execute("add_to_cart", {"product_id": TENT})
    await ex.execute("get_product_details", {"product_id": TEE})
    await ex.execute("add_to_cart", {"product_id": TEE_S})

    updated = await ex.execute("update_cart_item", {"product_id": TENT, "quantity": 2})
    assert not updated.is_error
    lines = {li["item"]["id"]: li["quantity"] for li in next(iter(store.carts.values()))["line_items"]}
    assert lines == {"gid://shopify/ProductVariant/201": 2, TEE_S: 1}

    removed = await ex.execute("remove_from_cart", {"product_id": TENT})
    assert not removed.is_error
    lines = {li["item"]["id"] for li in next(iter(store.carts.values()))["line_items"]}
    assert lines == {TEE_S}

    # Not in the cart: said so, nothing claimed.
    again = await ex.execute("remove_from_cart", {"product_id": TENT})
    assert again.is_error and "not in the cart" in again.result_text


async def test_a_family_id_with_two_sizes_in_the_cart_asks_which(store):
    ex = make_executor(make_backend(store))
    await ex.execute("search_products", {"query": "merino"})
    await ex.execute("get_product_details", {"product_id": TEE})
    await ex.execute("add_to_cart", {"product_id": TEE_S})
    await ex.execute("add_to_cart", {"product_id": TEE_L})
    out = await ex.execute("remove_from_cart", {"product_id": TEE})
    assert out.is_error and TEE_S in out.result_text and TEE_L in out.result_text
    assert len(next(iter(store.carts.values()))["line_items"]) == 2


async def test_an_unknown_product_id_is_not_found_not_an_outage(store):
    # Live: "Tell me about gid://shopify/Product/1" got "that lookup isn't working right now".
    backend = make_backend(store)
    session = ShoppingSessionContext(session_id="s1", user_id="guest-1", now=datetime(2026, 10, 6, 9))
    assert await backend.get_product_details(session, "gid://shopify/Product/999") is None
    out = await make_executor(backend).execute(
        "get_product_details", {"product_id": "gid://shopify/Product/999"}
    )
    assert "temporarily unavailable" not in out.result_text


async def test_removing_the_last_line_cancels_the_cart(store):
    ex = make_executor(make_backend(store))
    await ex.execute("search_products", {"query": "tent"})
    await ex.execute("add_to_cart", {"product_id": TENT})
    removed = await ex.execute("remove_from_cart", {"product_id": "gid://shopify/ProductVariant/201"})
    assert not removed.is_error
    assert not store.carts and any(c[0] == "cancel_cart" for c in store.calls)


async def test_policies_come_from_the_storefront_endpoint(store):
    ex = make_executor(make_backend(store))
    out = await ex.execute("search_policies", {"query": "returns"})
    assert "30 days" in out.result_text
    assert "shipping policy" not in out.result_text  # an answer found needs no fallback


async def test_a_policy_search_shopify_cannot_place_falls_back_to_the_general_policies(store):
    # Live, "Can I transfer a booking, any service fees?" comes back []; the return policy
    # is where the store says bookings and tickets are non-refundable.
    ex = make_executor(make_backend(store))
    out = await ex.execute("search_policies", {"query": "ticket transfer resale service fees"})
    assert not out.is_error
    assert "non-refundable" in out.result_text and "3-7 business days" in out.result_text


@pytest.mark.parametrize("country", [None, "DE"])
async def test_a_missing_or_unshipped_buyer_country_becomes_the_stores_own(country):
    # Seen live on Render: with no country (or one the store does not ship to) Shopify's cart
    # refused every shipped product as "already sold out". The store's /meta.json names its
    # country and the countries it ships to, so the backend uses those.
    store = FakeShopifyStore()
    ex = make_executor(make_backend(store, buyer_country=country))
    await ex.execute("search_products", {"query": "tent"})
    assert not (await ex.execute("add_to_cart", {"product_id": TENT})).is_error
    create = next(c for c in store.calls if c[0] == "create_cart")
    assert create[1]["cart"]["context"] == {"address_country": "US"}


async def test_a_cart_refusal_of_an_available_item_is_not_called_sold_out():
    # Without /meta.json and a country the cart still drops shipped goods; the catalog says
    # they are available, so the agent must not tell the shopper they are sold out.
    ex = make_executor(make_backend(FakeShopifyStore(meta=False), buyer_country=None))
    await ex.execute("search_products", {"query": "tent tour"})
    refused = await ex.execute("add_to_cart", {"product_id": TENT})
    assert refused.is_error and "do not call it sold out" in refused.result_text
    assert not (await ex.execute("add_to_cart", {"product_id": TOUR})).is_error

    # A variant the catalog itself marks out of stock is still reported as such.
    ex = make_executor(make_backend(FakeShopifyStore()))
    await ex.execute("search_products", {"query": "merino"})
    await ex.execute("get_product_details", {"product_id": TEE})
    oos = await ex.execute("add_to_cart", {"product_id": TEE_M})
    assert oos.is_error and "out of stock" in oos.result_text


def test_config_switches_off_what_shopify_has_no_tool_for():
    absent = shopify_agent_config().absent_tools()
    assert {"get_orders", "get_order_status", "get_fulfillment_options"} <= absent
    assert "checkout" not in absent


def test_shopify_notes_reach_the_prompt_whatever_the_search_notes():
    assert SHOPIFY_PROMPT_NOTES in shopify_agent_config().domain_search_notes
    custom = shopify_agent_config(domain_search_notes="We sell tours.").domain_search_notes
    assert custom.startswith("We sell tours.") and SHOPIFY_PROMPT_NOTES in custom


def test_descriptions_get_the_space_shopify_leaves_out_between_sentences():
    # Live description html, verbatim apart from length: no tags, no space between sentences.
    raw = {"html": "Guided coach tour with lunch.Experience with iKnowledge. Sizes S, M, L (L is out).Pick"}
    assert (
        _text(raw)
        == "Guided coach tour with lunch. Experience with iKnowledge. Sizes S, M, L (L is out). Pick"
    )
    assert _text("Version 2.5 of e.g. iKnowledge") == "Version 2.5 of e.g. iKnowledge"


async def test_an_add_in_the_same_round_as_a_search_waits_for_its_results(store):
    # Live: "add the tour too" searched for the tour and, in the same round, added the mug
    # the model had tried before. One function from ``execute`` is one round.
    ex = make_executor(make_backend(store))
    await ex.execute("search_products", {"query": "tent"})
    one_round = ex.execute
    await one_round("search_products", {"query": "tour"})
    held = await one_round("add_to_cart", {"product_id": TENT})
    assert held.is_error and "results are not back yet" in held.result_text and not store.carts
    assert not (await ex.execute("add_to_cart", {"product_id": TOUR})).is_error  # next round


async def test_a_cart_line_id_counts_as_seen(store):
    # The tent goes in by its product id and sits in the cart as variant 201; adding that id
    # again (from the cart panel or the session context) must pass the provenance gate.
    ex = make_executor(make_backend(store))
    await ex.execute("search_products", {"query": "tent"})
    await ex.execute("add_to_cart", {"product_id": TENT})
    again = await ex.execute("add_to_cart", {"product_id": "gid://shopify/ProductVariant/201"})
    assert not again.is_error
    lines = next(iter(store.carts.values()))["line_items"]
    assert [(li["item"]["id"], li["quantity"]) for li in lines] == [("gid://shopify/ProductVariant/201", 2)]
    # Variant ids from a product lookup are seen too.
    await ex.execute("get_product_details", {"product_id": TEE})
    assert not (await ex.execute("add_to_cart", {"product_id": TEE_S})).is_error


async def test_a_shopify_error_reaches_the_agent_in_the_stores_words(store, monkeypatch):
    # Live: any Shopify error became "add_to_cart is temporarily unavailable", and the agent
    # told the shopper the cart wasn't working.
    def broken_cart(body, args, request):
        error = {"type": "error", "code": "invalid_input", "content": "Quantity must be at most 10"}
        return store._result(body, {"messages": [error]}, True)

    monkeypatch.setattr(store, "_create_cart", broken_cart)
    ex = make_executor(make_backend(store))
    await ex.execute("search_products", {"query": "tent"})
    out = await ex.execute("add_to_cart", {"product_id": TENT})
    assert out.is_error and "Quantity must be at most 10" in out.result_text
    assert "temporarily unavailable" not in out.result_text


@pytest.mark.parametrize(
    ("question", "faq"),
    [
        ("contact", "How do I contact Trailhead customer support?"),
        ("I want to talk to a human", "How do I contact Trailhead customer support?"),
        ("exchange", "Can I refund or exchange a product?"),
        ("can I swap it for another size", "Can I refund or exchange a product?"),
    ],
)
async def test_policy_questions_in_other_words_still_find_the_faq(store, question, faq):
    # Live: "how do I contact support" found the contact FAQ but "contact" found nothing,
    # and "exchange" found nothing although the refund-or-exchange FAQ exists.
    backend = make_backend(store)
    found = await backend.search_policies(None, question)
    assert found and found[0].title == faq


async def test_a_ranking_word_with_no_ranking_data_falls_back_to_relevance(store):
    # Live: "bestseller" (sorted by rating) found nothing; the catalog has no sales data.
    backend = make_backend(store)
    filters = SearchFilters(sort="rating")
    assert [p.title for p in await backend.search_products(None, "bestseller", filters)]
    tents = await backend.search_products(None, "best selling tent")
    assert [p.title for p in tents] == ["Summit 2P Backpacking Tent"]


async def test_an_empty_cart_is_in_the_stores_currency():
    store = FakeShopifyStore()
    backend = make_backend(store)
    session = ShoppingSessionContext(session_id="s1", user_id="guest-1", now=datetime(2026, 10, 6, 9))
    assert (await backend.get_cart(session)).currency == "USD"

    class SgdStore(FakeShopifyStore):
        def _handle(self, request):
            if request.url.path == "/meta.json":
                return httpx.Response(
                    200, json={"country": "SG", "currency": "SGD", "ships_to_countries": ["SG"]}
                )
            return super()._handle(request)

    assert (await make_backend(SgdStore()).get_cart(session)).currency == "SGD"


async def test_shopify_rate_limits_are_retried_not_shown_to_the_shopper(store, monkeypatch):
    # Live: four chats at once got 429 on cart writes ("the store did not accept that").
    real, refusals = store._create_cart, []

    def busy_then_ok(body, args, request):
        if len(refusals) < 2:
            refusals.append(1)
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "Too many requests"})
        return real(body, args, request)

    monkeypatch.setattr(store, "_create_cart", busy_then_ok)
    ex = make_executor(make_backend(store))
    await ex.execute("search_products", {"query": "tent"})
    added = await ex.execute("add_to_cart", {"product_id": TENT})
    assert not added.is_error and len(refusals) == 2


async def test_catalog_reads_are_cached_briefly_and_cart_reads_never(store):
    backend = make_backend(store)
    ex = make_executor(backend)
    await ex.execute("search_products", {"query": "tent"})
    await ex.execute("search_products", {"query": "tent"})
    assert [c[0] for c in store.calls].count("search_catalog") == 1
    await ex.execute("add_to_cart", {"product_id": TENT})
    backend._written.clear()  # past the moments after our own write
    await ex.execute("get_cart", {})
    await ex.execute("get_cart", {})
    assert [c[0] for c in store.calls].count("get_cart") == 2


async def test_a_cart_just_written_is_not_read_back_before_the_next_change(store):
    # Each add used to read the cart, then write it: two calls, and the busy store's
    # rate limit hit the read first.
    ex = make_executor(make_backend(store))
    await ex.execute("search_products", {"query": "tent tour"})
    await ex.execute("add_to_cart", {"product_id": TENT})
    reads = [c[0] for c in store.calls].count("get_cart")
    await ex.execute("add_to_cart", {"product_id": TOUR})
    await ex.execute("update_cart_item", {"product_id": TENT, "quantity": 2})
    assert [c[0] for c in store.calls].count("get_cart") == reads
    lines = {li["item"]["id"]: li["quantity"] for li in next(iter(store.carts.values()))["line_items"]}
    assert lines == {"gid://shopify/ProductVariant/201": 2, "gid://shopify/ProductVariant/401": 1}
