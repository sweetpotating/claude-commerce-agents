"""The recommendation card's buttons: choose a variant, add to cart, check out, each
without a model turn but through the agent's own gates."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from my_store import app as app_module


@pytest.fixture
def client() -> TestClient:
    return TestClient(app_module.app)


def start(client: TestClient) -> tuple[str, dict[str, str]]:
    sid = client.post("/api/session", json={}).json()["session_id"]
    return sid, {"x-session-id": sid}


def shown(sid: str, query: str) -> None:
    """What a recommendation turn leaves behind: the agent searched and showed results."""
    s = app_module.SESSIONS[sid]
    asyncio.run(app_module.executor_for(s).execute("search_products", {"query": query}))


def test_recommendation_to_checkout_without_a_model_turn(client):
    sid, h = start(client)
    shown(sid, "merino tee")

    # The tee has sizes: the card asks for its variants, sold-out ones marked.
    opts = client.post("/api/product/options", json={"product_id": "TS-100"}, headers=h).json()
    sizes = {v["option_values"]["size"]: v for v in opts["variants"]}
    assert sizes["M"]["in_stock"] is False and sizes["L"]["price"] == 52.0

    # Sold out is refused with a reason; an in-stock size goes in.
    refused = client.post("/api/cart/add", json={"product_id": "TS-102"}, headers=h)
    assert refused.status_code == 400 and refused.json()["detail"]
    cart = client.post("/api/cart/add", json={"product_id": "TS-103"}, headers=h).json()
    assert cart["item_count"] == 1 and cart["subtotal"] == 52.0

    # The family itself can't go in the cart: a size is needed.
    assert client.post("/api/cart/add", json={"product_id": "TS-100"}, headers=h).status_code == 400

    card = client.post("/api/checkout", headers=h).json()
    assert card["handoffs"][0]["url"].startswith("http")
    # The agent hears what the shopper did on its next turn.
    events = app_module.SESSIONS[sid].pending_app_events
    assert any("Add to cart" in e for e in events) and any("Checkout" in e for e in events)


def test_buttons_only_act_on_products_the_chat_showed(client):
    _, h = start(client)
    assert client.post("/api/product/options", json={"product_id": "TS-100"}, headers=h).status_code == 400
    assert client.post("/api/cart/add", json={"product_id": "TS-103"}, headers=h).status_code == 400
    assert client.post("/api/checkout", headers=h).status_code == 400  # empty cart
    assert client.post("/api/cart/add", json={"product_id": "TS-103"}).status_code == 401


def test_product_page_opens_inside_the_chat(client):
    sid, h = start(client)
    shown(sid, "merino tee")
    page = client.post("/api/product", json={"product_id": "TS-100"}, headers=h).json()
    assert page["title"] == "Ridgeline Merino Tee" and page["description"]
    assert {v["option_values"]["size"] for v in page["variants"]} == {"S", "M", "L"}
    assert "image_urls" not in page and isinstance(page["images"], list)
    # The page counts as a lookup: a size picked on it goes straight into the cart.
    assert client.post("/api/cart/add", json={"product_id": "TS-101"}, headers=h).status_code == 200


def test_a_chat_opened_on_a_product_page_can_add_that_product_straight_away(client):
    # The bubble opened on the tent's page: the opening card shows it, and adding it needs
    # no search first (the page's product counts as seen).
    sid, h = start(client)
    page = {"page_type": "product", "product_id": "TS-200"}
    opened = client.post("/api/page", json=page, headers=h).json()
    assert opened["product"]["title"] == "Summit 2P Backpacking Tent"
    added = client.post("/api/cart/add", json={"product_id": "TS-200"}, headers=h)
    assert added.status_code == 200 and added.json()["item_count"] == 1
    # Not a product page, or a product the store doesn't have: no card, no error.
    assert client.post("/api/page", json={"page_type": "home"}, headers=h).json() == {"product": None}
    assert client.post(
        "/api/page", json={"page_type": "product", "product_id": "NOPE-1"}, headers=h
    ).json() == {"product": None}
