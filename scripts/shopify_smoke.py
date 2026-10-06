"""Live check of ShopifyUCPBackend against a real store, through the agent's tool executor.

    SHOPIFY_STORE_DOMAIN=your-store.myshopify.com python -m scripts.shopify_smoke --query "shirt"

Steps: the store's /.well-known/ucp, search, product details (variants), add an in-stock
variant, add a plain product, read the cart, a policy question, the checkout card's link.
Each step prints what came back; the script exits non-zero at the first failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime

import httpx
from commerce_common.memory import InMemoryMemoryStore
from commerce_common.skills import SkillRegistry
from shopping_agent import ShoppingSessionContext, ShoppingSessionState
from shopping_agent.executor import ShoppingToolExecutor, build_memory

from my_store.shopify_backend import ShopifyUCPBackend, shopify_agent_config


def step(title: str, ok: bool, detail: str) -> None:
    print(f"\n[{'PASS' if ok else 'FAIL'}] {title}\n{detail[:1500]}")
    if not ok:
        sys.exit(1)


async def main(query: str) -> None:
    domain = os.environ["SHOPIFY_STORE_DOMAIN"]
    async with httpx.AsyncClient(timeout=20) as http:
        well_known = await http.get(f"https://{domain}/.well-known/ucp")
        step("store publishes /.well-known/ucp", well_known.status_code == 200, well_known.text)

    backend = ShopifyUCPBackend.from_env()
    config = shopify_agent_config()
    state = ShoppingSessionState()
    ex = ShoppingToolExecutor(
        backend=backend,
        config=config,
        skills=SkillRegistry([]),
        session=ShoppingSessionContext(session_id="smoke", user_id="smoke", now=datetime.now()),
        state=state,
        memory=build_memory(config, InMemoryMemoryStore()),
    )
    # No real shopper here: a documentation-range address lets authenticated checkout run.
    backend.set_buyer_ip("smoke", os.environ.get("SHOPIFY_SMOKE_BUYER_IP", "203.0.113.7"))

    found = await ex.execute("search_products", {"query": query})
    step(f"search_products({query!r})", not found.is_error and bool(state.seen_products), found.result_text)

    family = next((p for p in state.seen_products.values() if p.options), None)
    plain = next((p for p in state.seen_products.values() if not p.options), None)
    if family:
        details = await ex.execute("get_product_details", {"product_id": family.product_id})
        variants = [p for p in state.seen_products.values() if p.variant_of == family.product_id]
        step(
            f"get_product_details({family.title}): {len(variants)} variants",
            not details.is_error and bool(variants),
            details.result_text,
        )
        in_stock = next((v for v in variants if v.in_stock), None)
        if in_stock:
            added = await ex.execute("add_to_cart", {"product_id": in_stock.product_id})
            step(f"add_to_cart(variant {in_stock.option_values})", not added.is_error, added.result_text)
    else:
        print("\n[SKIP] no product with options in these results; try another --query")
    if plain:
        added = await ex.execute("add_to_cart", {"product_id": plain.product_id})
        step(f"add_to_cart(plain product {plain.title})", not added.is_error, added.result_text)

    cart = await ex.execute("get_cart", {})
    step("get_cart", not cart.is_error and "subtotal" in cart.result_text, cart.result_text)

    policy = await ex.execute("search_policies", {"query": "return policy"})
    step("search_policies('return policy')", not policy.is_error, policy.result_text)

    checkout = await ex.execute("checkout", {})
    card = next((e.data["payload"] for e in checkout.events if e.type == "ui"), {})
    handoffs = card.get("handoffs") or []
    step("checkout card has a Shopify checkout link", bool(handoffs), json.dumps(card, indent=1))
    print(f"\nOpen this to confirm the cart arrives at Shopify checkout:\n{handoffs[0]['url']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="shirt", help="a word that matches products in the store")
    asyncio.run(main(parser.parse_args().query))
