"""ShopifyUCPBackend against the fake store, driven through the reference tool executor so
the agent's gates (provenance, options) run exactly as they do in a live turn."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from commerce_common.memory import InMemoryMemoryStore
from commerce_common.skills import SkillRegistry
from shopping_agent import ShoppingSessionContext, ShoppingSessionState
from shopping_agent.executor import ShoppingToolExecutor, build_memory
from shopping_agent.gates import OPTIONS_GATE, PROVENANCE_GATE

from my_store.shopify_backend import SHOPIFY_PROMPT_NOTES, ShopifyUCPBackend, shopify_agent_config

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


def make_executor(backend: ShopifyUCPBackend) -> ShoppingToolExecutor:
    config = shopify_agent_config(brand_name="Trailhead")
    return ShoppingToolExecutor(
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


async def test_without_a_buyer_country_shopify_calls_physical_goods_sold_out(store):
    # Seen live: with no country (or one the store does not ship to) every shipped product is
    # "already sold out" at the cart while search says available; vouchers still add.
    ex = make_executor(make_backend(store, buyer_country=None))
    await ex.execute("search_products", {"query": "tent tour"})
    refused = await ex.execute("add_to_cart", {"product_id": TENT})
    assert refused.is_error and "already sold out" in refused.result_text
    assert not (await ex.execute("add_to_cart", {"product_id": TOUR})).is_error

    ex = make_executor(make_backend(FakeShopifyStore(), buyer_country="US"))
    await ex.execute("search_products", {"query": "tent"})
    assert not (await ex.execute("add_to_cart", {"product_id": TENT})).is_error


def test_config_switches_off_what_shopify_has_no_tool_for():
    absent = shopify_agent_config().absent_tools()
    assert {"get_orders", "get_order_status", "get_fulfillment_options"} <= absent
    assert "checkout" not in absent


def test_shopify_notes_reach_the_prompt_whatever_the_search_notes():
    assert SHOPIFY_PROMPT_NOTES in shopify_agent_config().domain_search_notes
    custom = shopify_agent_config(domain_search_notes="We sell tours.").domain_search_notes
    assert custom.startswith("We sell tours.") and SHOPIFY_PROMPT_NOTES in custom
