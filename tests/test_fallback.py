"""When the model is unavailable the chat keeps selling from the store's own data: FAQ
answers for policy questions, product cards for everything else, and those cards' Add to
cart buttons work (seen live: out of API credit, the chat could only say "unavailable")."""

from __future__ import annotations

import json

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from my_store import app as host
from my_store.fallback import keywords


def _events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            out.append((lines["event"], json.loads(lines.get("data", "{}"))))
    return out


@pytest.fixture
def chat(monkeypatch):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    async def no_credit(messages, ctx, state):
        raise anthropic.BadRequestError(
            "Your credit balance is too low to access the Anthropic API.",
            response=httpx.Response(400, request=request),
            body=None,
        )
        yield  # an async generator, like the real stream_turn

    monkeypatch.setattr(host.agent, "stream_turn", no_credit)
    client = TestClient(host.app)
    sid = client.post("/api/session", json={}).json()["session_id"]

    def send(message: str):
        r = client.post("/api/chat", headers={"x-session-id": sid}, json={"message": message})
        return _events(r.text)

    return client, sid, send


def test_a_search_still_shows_products_that_can_be_added(chat):
    client, sid, send = chat
    events = send("I'm looking for a tent")
    cards = [d for e, d in events if e == "ui" and d["component"] == "products"]
    assert cards, events
    titles = [i["product"]["title"] for i in cards[0]["payload"]["items"]]
    assert any("Tent" in t for t in titles)
    text = "".join(d["text"] for e, d in events if e == "text_delta")
    assert "unavailable right now" in text and "'tent'" in text
    assert not [e for e, _ in events if e == "error"]
    tent = next(
        i["product"]["product_id"] for i in cards[0]["payload"]["items"] if "2P" in i["product"]["title"]
    )
    added = client.post("/api/cart/add", headers={"x-session-id": sid}, json={"product_id": tent})
    assert added.status_code == 200 and added.json()["item_count"] == 1
    chips = [d["payload"]["suggestions"] for e, d in events if e == "ui" and d["component"] == "suggestions"]
    assert chips and "Show more tent" in chips[-1]


def test_a_policy_question_gets_the_stores_own_answer(chat):
    _, _, send = chat
    events = send("What is your return policy?")
    text = "".join(d["text"] for e, d in events if e == "text_delta")
    assert "store's own pages" in text and "return" in text.lower()
    assert not [d for e, d in events if e == "ui" and d["component"] == "products"]


def test_nothing_matching_is_said_plainly_with_other_products_offered(chat):
    _, _, send = chat
    events = send("do you sell surfboards?")
    text = "".join(d["text"] for e, d in events if e == "text_delta")
    assert "nothing matching 'surfboards'" in text
    assert [d for e, d in events if e == "ui" and d["component"] == "products"]


def test_keywords_keep_product_words_only():
    assert keywords("I need a gift for my mum who loves music") == ["music"]
    assert keywords("do you sell surfboards?") == ["surfboards"]
    assert keywords("cheap travel adapter") == ["adapter", "travel"]
