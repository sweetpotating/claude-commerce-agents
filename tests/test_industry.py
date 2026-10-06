"""The vertical extensions (industry.py) on the Shopify backend: the itinerary and plan
table fill every product from this session's results and refuse ids it never saw."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from commerce_common.memory import InMemoryMemoryStore
from commerce_common.skills import SkillRegistry
from shopping_agent import ShoppingSessionContext, ShoppingSessionState
from shopping_agent.executor import ShoppingToolExecutor, build_memory
from shopping_agent.tools.registry import build_tools

from my_store.industry import POLICY_TERMS, industry_config_overrides, industry_extensions
from my_store.shopify_backend import ShopifyUCPBackend, shopify_agent_config

from .fake_shopify import SHOP, FakeShopifyStore

TEE, TENT = "gid://shopify/Product/1", "gid://shopify/Product/2"


@pytest.fixture
def executor() -> ShoppingToolExecutor:
    backend = ShopifyUCPBackend(
        SHOP,
        agent_profile_url="https://agent.example/ucp-profile.json",
        http=httpx.AsyncClient(transport=FakeShopifyStore().transport()),
    )
    config = shopify_agent_config(**industry_config_overrides())
    return ShoppingToolExecutor(
        backend=backend,
        config=config,
        skills=SkillRegistry([]),
        session=ShoppingSessionContext(session_id="s1", user_id="guest-1", now=datetime(2026, 10, 6, 9)),
        state=ShoppingSessionState(),
        memory=build_memory(config, InMemoryMemoryStore()),
        extensions=industry_extensions(),
    )


def test_config_carries_every_verticals_vocabulary():
    config = shopify_agent_config(**industry_config_overrides())
    for term in ("roaming", "booking", "resale", "service fee", "return"):
        assert term in config.policy_intent_terms
    assert set(POLICY_TERMS) <= set(config.policy_intent_terms)
    assert config.max_search_results == 25 and config.max_quantity_per_item == 8
    names = {t["name"] for t in build_tools(config, [], industry_extensions())}
    assert {"present_itinerary", "present_plan_comparison"} <= names


async def test_itinerary_fills_days_from_seen_products(executor):
    await executor.execute("search_products", {"query": "merino tee tent"})
    out = await executor.execute(
        "present_itinerary",
        {
            "title": "Weekend away",
            "travel_dates": "Oct 17-19",
            "days": [
                {"label": "Day 1: Arrive", "product_ids": [TENT, "gid://shopify/Product/999"]},
                {"label": "Day 2: Hike", "note": "Early start.", "product_ids": [TEE]},
            ],
        },
    )
    assert not out.is_error
    card = next(e for e in out.events if e.type == "ui").data
    assert card["component"] == "itinerary"
    day1, day2 = card["payload"]["days"]
    assert [p["product_id"] for p in day1["products"]] == [TENT]  # unseen id dropped
    assert day2["note"] == "Early start." and day2["products"][0]["title"] == "Ridgeline Merino Tee"


async def test_plan_table_rows_come_from_the_catalog(executor):
    refused = await executor.execute("present_plan_comparison", {"plan_ids": [TEE, TENT]})
    assert refused.is_error  # nothing searched yet

    await executor.execute("search_products", {"query": "merino tee tent"})
    out = await executor.execute(
        "present_plan_comparison",
        {
            "plan_ids": [TEE, TENT],
            "recommended_plan_id": TENT,
            "annotations": [{"plan_id": TEE, "best_for": "day trips"}],
        },
    )
    assert not out.is_error
    payload = next(e for e in out.events if e.type == "ui").data["payload"]
    rows = {r["label"]: r["values"] for r in payload["rows"]}
    assert rows["Price"][0].startswith("48") and rows["Size"] == ["S, M, L", "—"]
    assert payload["recommended_plan_id"] == TENT
    assert payload["annotations"] == [{"plan_id": TEE, "best_for": "day trips"}]


async def test_every_product_links_to_the_storefront(executor):
    found = await executor.execute("search_products", {"query": "merino tee tent"})
    assert f"https://{SHOP}/search?q=Ridgeline+Merino+Tee" in found.result_text
    out = await executor.execute("present_plan_comparison", {"plan_ids": [TEE, TENT]})
    payload = next(e for e in out.events if e.type == "ui").data["payload"]
    assert all(p["attributes"]["product_url"].startswith(f"https://{SHOP}/") for p in payload["plans"])
    assert "Product url" not in {r["label"] for r in payload["rows"]}  # a link, not a row


def test_discovery_rules_reach_the_prompt():
    notes = shopify_agent_config(**industry_config_overrides()).domain_search_notes
    assert "Recommendations lead to products" in notes and "search the catalog for each step" in notes


def test_the_agent_recommends_before_asking():
    from commerce_common.skills import SkillRegistry

    from my_store.app import SKILLS_DIR

    assert SKILLS_DIR.name == "skills" and "vendor" not in SKILLS_DIR.parts
    text = " ".join(
        (SKILLS_DIR / n / "SKILL.md").read_text() for n in SkillRegistry.from_dir(SKILLS_DIR).names
    )
    assert "spend one turn on two or three short questions" not in text  # the intake turn is gone
    assert "Never send a plan without products" in text
    notes = shopify_agent_config(**industry_config_overrides()).domain_search_notes
    assert "do not ask a question before showing options" in notes
