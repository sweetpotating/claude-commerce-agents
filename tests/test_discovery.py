"""The products-first rule: a shopping request's first round is pinned to search_products,
after the reference rules, so terms and order questions still go to their own tools."""

from __future__ import annotations

import pytest
from commerce_common.grounding import first_forced_tool
from shopping_agent import ShoppingSessionState
from shopping_agent_runtime import orchestrator

from my_store import app as host  # installs the rule
from my_store.discovery import shopping_request


@pytest.mark.parametrize(
    "text",
    [
        "I need a gift",
        "help me plan a trip",
        "Compare two tours",
        "Mt Fuji vs Kuala Lumpur",
        "what phone plan should I get?",
        "do you sell eSIMs?",
        "something for a rainy day in Tokyo",
        "I'm going to Japan",
        "any ideas for my mum?",
        "what's good for a first-time visitor to Singapore?",
    ],
)
def test_shopping_requests_search_first(text):
    tool = first_forced_tool(orchestrator.GROUNDING_RULES, host.agent.config, text, ShoppingSessionState())
    assert tool == "search_products"


@pytest.mark.parametrize(
    "text",
    [
        "add the tee to my cart",
        "thanks, that's all",
        "yes please",
        "remember I'm vegetarian",
        "remove the SIM",
        "[App events since your last reply: Customer tapped Add to cart on Airport Transfer.]",
    ],
)
def test_other_messages_are_left_alone(text):
    assert not shopping_request(text)


def test_terms_questions_still_read_the_policies_first():
    text = "how many days do I have to return an item?"
    tool = first_forced_tool(orchestrator.GROUNDING_RULES, host.agent.config, text, ShoppingSessionState())
    assert tool == "search_policies"


def test_a_tapped_chip_always_searches():
    from my_store.discovery import chip_tapped

    state = ShoppingSessionState()
    text = "Surprise me"  # no shopping cue in the wording

    def forced(t, st):
        return first_forced_tool(orchestrator.GROUNDING_RULES, host.agent.config, t, st)

    assert forced(text, state) is None
    chip_tapped(state)
    assert forced(text, state) == "search_products"
    # Only that turn; and a cart chip doesn't search.
    assert forced(text, state) is None
    chip_tapped(state)
    assert forced("Add the SIM to my cart", state) is None


def test_fallback_chips_come_from_the_products_shown():
    from my_store.discovery import fallback_chips, product_titles

    payload = {
        "steps": [
            {"products": [{"title": "Mt Fuji Day Trip from Tokyo"}]},
            {"products": [{"title": "Japan eSIM – 7 Days 10GB"}]},
        ]
    }
    titles = product_titles("plan", payload)
    assert titles == ["Mt Fuji Day Trip from Tokyo", "Japan eSIM – 7 Days 10GB"]
    chips = fallback_chips(titles)
    assert chips[:2] == ["Compare Mt Fuji Day Trip and Japan eSIM", "Show more like Mt Fuji Day Trip"]
    assert len(chips) == 4 and len(fallback_chips([])) == 4


async def test_every_reply_ends_with_chips(monkeypatch):
    from commerce_common.streaming import AgentEvent

    async def quiet_turn(messages, ctx, state):
        yield AgentEvent.text_delta("Here you go.")
        yield AgentEvent.ui("products", {"items": [{"product": {"title": "Merino Tee"}}]})

    async def no_memory(*_):
        return None

    monkeypatch.setattr(host.agent, "stream_turn", quiet_turn)
    monkeypatch.setattr(host.agent, "update_memory", no_memory)
    from fastapi.testclient import TestClient

    client = TestClient(host.app)
    sid = client.post("/api/session", json={}).json()["session_id"]
    body = client.post(
        "/api/chat", json={"message": "tees", "source": "chip"}, headers={"x-session-id": sid}
    ).text
    last = [b for b in body.split("\n\n") if b.startswith("event: ui")][-1]
    assert '"suggestions"' in last and "Show more like Merino Tee" in last
