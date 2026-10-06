"""A tapped search chip always ends on products: when the model's reply showed none (live:
"Show more Singapore tours" found nothing new and answered in text only), the host adds the
closest products found, above the chips. A cart chip gets no cards."""

from __future__ import annotations

import json

import pytest
from commerce_common.streaming import AgentEvent
from fastapi.testclient import TestClient
from shopping_agent import Product

from my_store import app as host


def events(response) -> list[tuple[str, dict]]:
    out = []
    for block in response.text.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            out.append((lines["event"], json.loads(lines.get("data", "{}"))))
    return out


@pytest.fixture
def chat(monkeypatch):
    async def text_only_turn(messages, ctx, state):
        yield AgentEvent.text_delta("That's everything the store has for that.")
        yield AgentEvent.ui("suggestions", {"suggestions": ["Show more tours"]})

    async def no_memory(*_):
        return None

    monkeypatch.setattr(host.agent, "stream_turn", text_only_turn)
    monkeypatch.setattr(host.agent, "update_memory", no_memory)
    client = TestClient(host.app)
    sid = client.post("/api/session", json={}).json()["session_id"]
    host.SESSIONS[sid].state.remember_products(
        [
            Product(product_id="TOUR-1", title="City Tour", price=60),
            Product(product_id="TOUR-1-V", title="City Tour", price=60, variant_of="TOUR-1"),
            Product(product_id="PASS-2", title="City Pass", price=89),
        ]
    )

    def send(message: str, source: str | None = "chip"):
        body = {"message": message, **({"source": source} if source else {})}
        return events(client.post("/api/chat", headers={"x-session-id": sid}, json=body))

    return send


def test_a_search_chip_with_no_products_gets_the_closest_cards_above_its_chips(chat):
    out = [(e, d) for e, d in chat("Show more Singapore tours") if e == "ui"]
    assert [d["component"] for _, d in out] == ["products", "suggestions"]
    cards = out[0][1]["payload"]
    assert cards["title"] == "Closest matches"
    assert [i["product"]["product_id"] for i in cards["items"]] == [
        "PASS-2",
        "TOUR-1",
    ]  # newest first, no variants


def test_a_cart_chip_or_a_typed_message_gets_no_fallback_cards(chat):
    assert [d["component"] for e, d in chat("Add the City Pass to cart") if e == "ui"] == ["suggestions"]
    assert [d["component"] for e, d in chat("Show more tours", source=None) if e == "ui"] == ["suggestions"]
