"""Offline end-to-end walkthrough: discovery -> details -> cart -> fulfillment -> checkout.

Runs the reference ShoppingToolExecutor (the same code the model's tool calls hit on every
runtime path) against MyStoreBackend, playing the tool calls a model would make. No API key
needed. It also shows the safety gates refusing what the model must not do.

    python -m scripts.walkthrough      # from the repo root
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from commerce_common.memory import InMemoryMemoryStore
from commerce_common.skills import SkillRegistry
from shopping_agent import (
    ShoppingAgentConfig,
    ShoppingSessionContext,
    ShoppingSessionState,
)
from shopping_agent.executor import ShoppingToolExecutor, build_memory

from my_store.backend import MyStoreBackend

SKILLS_DIR = Path(__file__).resolve().parents[1] / "vendor" / "commerce-agents" / "shopping-agent" / "skills"


def show(step: str, tool: str, args: dict, outcome) -> None:
    print(f"\n=== {step}: {tool}({json.dumps(args)})")
    status = "BLOCKED by " + outcome.blocked if outcome.blocked else ("ERROR" if outcome.is_error else "ok")
    print(f"--- status: {status}")
    text = outcome.result_text
    print(text if len(text) < 1400 else text[:1400] + " ...")
    for event in outcome.events:
        if event.type in ("ui", "cart_update"):
            print(f"--- host event [{event.type}]: {json.dumps(event.data, default=str)[:600]}")


async def main() -> None:
    config = ShoppingAgentConfig(brand_name="Trailhead Supply", assistant_name="Trail Guide")
    skills = SkillRegistry.from_dir(SKILLS_DIR) if SKILLS_DIR.exists() else SkillRegistry([])
    backend = MyStoreBackend()
    session = ShoppingSessionContext(session_id="sess-demo", user_id="demo-user", now=datetime.now())
    state = ShoppingSessionState()  # the provenance record your host stores per session
    executor = ShoppingToolExecutor(
        backend=backend,
        config=config,
        skills=skills,
        session=session,
        state=state,
        memory=build_memory(config, InMemoryMemoryStore()),
    )

    async def call(step: str, tool: str, args: dict):
        outcome = await executor.execute(tool, args)
        show(step, tool, args, outcome)
        return outcome

    # 0. Guardrail: the model cannot add an id it never saw this session.
    await call("0 provenance gate", "add_to_cart", {"product_id": "TS-200"})

    # 1. Discovery
    await call(
        "1 discovery",
        "search_products",
        {"query": "two person backpacking tent", "filters": {"max_price": 250}},
    )
    await call("1b discovery", "search_products", {"query": "merino tee"})
    await call(
        "1c render",
        "present_products",
        {"picks": [{"product_id": "TS-200", "reason": "Two-person and under $250"}]},
    )

    # 2. Research a product with options
    await call("2 options gate", "add_to_cart", {"product_id": "TS-100"})
    await call("2b details", "get_product_details", {"product_id": "TS-100"})

    # 3. Cart
    await call("3 out of stock", "add_to_cart", {"product_id": "TS-102"})
    await call("3b add variant", "add_to_cart", {"product_id": "TS-103"})
    await call("3c add tent", "add_to_cart", {"product_id": "TS-200"})

    # 4. Fulfillment + policy grounding
    await call("4 fulfillment", "get_fulfillment_options", {"product_ids": ["TS-200", "TS-103"]})
    await call("4b policy", "search_policies", {"query": "returns"})

    # 5. Checkout: stages the cart; the handoff URL is added server-side, never by the model.
    await call("5 checkout", "checkout", {"fulfillment_method": "shipping"})

    # 6. Payment completes on your hosted checkout -> your webhook records the order.
    token = next(iter(backend.checkout_tokens))
    order = backend.record_paid_order(token)
    print(f"\n=== 6 payment webhook: order {order.order_id} total ${order.total}")
    await call("6b post-purchase", "get_order_status", {"order_id": order.order_id})


if __name__ == "__main__":
    asyncio.run(main())
