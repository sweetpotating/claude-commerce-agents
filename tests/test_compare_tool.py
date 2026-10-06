"""The comparison card a client renders: a value in every cell, grounded ids only, no empty
first frame, and "these two" / "this" resolved. Each test is one finding from the compare
deep-dive (items 13-19)."""

from __future__ import annotations

import json

import pytest
from commerce_common.presentation import EnrichmentContext, PresentationRefused
from commerce_common.streaming import AgentEvent
from fastapi.testclient import TestClient
from shopping_agent import Product, ShoppingSessionState

from my_store import app as host
from my_store import compare, discovery
from my_store.industry import industry_config_overrides

FUJI = Product(
    product_id="FUJI",
    title="Mt Fuji Day Trip from Tokyo",
    price=110,
    currency="SGD",
    attributes={"Duration": "Full day", "Includes": "lunch", "product_url": "https://x"},
)
KL = Product(
    product_id="KL",
    title="Kuala Lumpur City Tour",
    price=45,
    currency="SGD",
    attributes={"Duration": "Half day"},
)
MUG = Product(product_id="MUG", title="Bookworm Mug", price=24.9, currency="SGD", in_stock=False)


def state(*products: Product) -> ShoppingSessionState:
    s = ShoppingSessionState()
    s.remember_products(list(products))
    return s


async def enrich(payload: dict, *products: Product) -> dict:
    ctx = EnrichmentContext(backend=None, config=None, session=None, state=state(*products))
    return await compare.enrich(compare.Payload.model_validate(payload), ctx)


# 13: every dimension has a value for every entry.
async def test_every_entry_has_a_value_for_every_dimension():
    out = await enrich(
        {
            "entries": [{"product_id": "FUJI"}, {"product_id": "KL", "values": {"Pickup": "Hotel lobby"}}],
            "dimensions": ["cost", "Duration", "Pickup", "Wifi"],
        },
        FUJI,
        KL,
    )
    assert out["dimensions"] == ["Price", "Duration", "Pickup", "Includes"]  # Wifi: nobody states it
    fuji, kl = (e["values"] for e in out["entries"])
    assert fuji == {"Price": "SGD 110.00", "Duration": "Full day", "Pickup": "—", "Includes": "lunch"}
    assert kl == {"Price": "SGD 45.00", "Duration": "Half day", "Pickup": "Hotel lobby", "Includes": "—"}
    assert all(set(e["values"]) == set(out["dimensions"]) for e in out["entries"])
    assert out["price_delta"]["amount"] == 65


async def test_the_store_fact_wins_over_the_models_value_and_stock_shows_when_it_differs():
    out = await enrich(
        {"entries": [{"product_id": "KL", "values": {"Duration": "2 days"}}, {"product_id": "MUG"}]}, KL, MUG
    )
    kl, mug = (e["values"] for e in out["entries"])
    assert kl["Duration"] == "Half day"
    assert mug["Availability"] == "Sold out" and kl["Availability"] == "In stock"


# 17: an id from nowhere refuses the whole call, naming it.
async def test_an_unseen_id_refuses_the_comparison():
    with pytest.raises(PresentationRefused, match="GHOST"):
        await enrich(
            {"entries": [{"product_id": "FUJI"}, {"product_id": "KL"}, {"product_id": "GHOST"}]}, FUJI, KL
        )
    with pytest.raises(PresentationRefused):
        await enrich({"entries": [{"product_id": "FUJI"}, {"product_id": "FUJI"}]}, FUJI)


# 14: no frame until two entries resolve, then a frame with cells.
def test_no_streamed_frame_before_two_entries():
    s = state(FUJI, KL)
    assert compare.partial({"title": "Fuji vs KL", "entries": [], "dimensions": []}, s) is None
    assert compare.partial({"title": "Fuji vs KL", "entries": [{"product_id": "FUJI"}]}, s) is None
    frame = compare.partial({"entries": [{"product_id": "FUJI"}, {"product_id": "KL"}]}, s)
    assert [e["product_id"] for e in frame["entries"]] == ["FUJI", "KL"]
    assert frame["entries"][1]["values"]["Duration"] == "Half day"


def test_the_model_sees_values_in_the_tool_schema_and_the_spec_is_installed():
    tool = next(t for t in host.agent._tools if t["name"] == "present_comparison")
    assert "values" in tool["input_schema"]["properties"]["entries"]["items"]["properties"]
    assert host.agent._specs["present_comparison"] is compare.SPEC


# 15: "compare these two" compares what was shown, and a card never comes alone.
def test_compare_these_two_needs_no_new_search():
    for text in (
        "compare these two",
        "Compare them",
        "what's the difference between these?",
        "which is better of the two",
    ):
        assert compare.refers_to_shown(text), text
        assert discovery._discovery(None, text, ShoppingSessionState()) is None
    for text in (
        "compare this with the mug",
        "compare these two with the Osaka tour",
        "compare japan and malaysia",
    ):
        assert not compare.refers_to_shown(text), text


def test_summary_line_from_the_table():
    payload = {
        "dimensions": ["Price", "Duration", "Includes"],
        "price_delta": {"amount": 65, "low_product_id": "KL", "high_product_id": "FUJI"},
        "entries": [
            {
                "product_id": "FUJI",
                "product": FUJI.model_dump(),
                "values": {"Duration": "Full day", "Includes": "lunch"},
            },
            {
                "product_id": "KL",
                "product": KL.model_dump(),
                "values": {"Duration": "Half day", "Includes": "—"},
            },
        ],
    }
    line = compare.summary(payload)
    assert "Kuala Lumpur City Tour is SGD 65.00 less than Mt Fuji Day Trip from Tokyo" in line
    assert line.endswith("They differ on duration (Full day vs Half day).")  # not "lunch vs —"


def _events(response) -> list[tuple[str, dict]]:
    out = []
    for block in response.text.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            out.append((lines["event"], json.loads(lines.get("data", "{}"))))
    return out


@pytest.fixture
def chat(monkeypatch):
    seen: list = []

    async def turn(messages, ctx, state):
        seen.append(ctx.page.extra)
        if "compare" in messages[-1]["content"]:
            payload = await enrich({"entries": [{"product_id": "FUJI"}, {"product_id": "KL"}]}, FUJI, KL)
            yield AgentEvent.ui("comparison", payload)  # a card and no text
        else:
            yield AgentEvent.text_delta("Two tours:")
            yield AgentEvent.ui(
                "products", {"items": [{"product": FUJI.model_dump()}, {"product": KL.model_dump()}]}
            )

    async def no_memory(*_):
        return None

    monkeypatch.setattr(host.agent, "stream_turn", turn)
    monkeypatch.setattr(host.agent, "update_memory", no_memory)
    client = TestClient(host.app)
    sid = client.post("/api/session", json={}).json()["session_id"]
    host.SESSIONS[sid].state.remember_products([FUJI, KL, MUG])

    def send(message: str, page: dict | None = None):
        body = {"message": message, **({"page": page} if page else {})}
        return _events(client.post("/api/chat", headers={"x-session-id": sid}, json=body))

    return send, seen


def test_these_means_the_last_shown_and_a_bare_card_gets_a_line(chat):
    send, seen = chat
    send("show me tours")
    out = send("compare these two")
    assert seen[1]["last_shown"] == [
        {"product_id": "FUJI", "title": "Mt Fuji Day Trip from Tokyo"},
        {"product_id": "KL", "title": "Kuala Lumpur City Tour"},
    ]
    text = "".join(d["text"] for e, d in out if e == "text_delta")
    assert "SGD 65.00 less" in text
    assert out[-1][1]["component"] == "suggestions"  # chips after the line, not before


# 16: "this" on a product page is that product.
def test_this_means_the_product_page_open(chat):
    send, seen = chat
    send("compare this with the mug", page={"page_type": "product", "product_id": "FUJI"})
    assert seen[0]["viewing"] == {
        "product_id": "FUJI",
        "title": "Mt Fuji Day Trip from Tokyo",
        "price": "SGD 110.00",
    }


# 18: comparison wording gets a comparison even when the model picks present_products.
async def test_difference_between_routes_to_the_comparison_card():
    s = host.Session(session_id="t", user_id="u")
    s.state.remember_products([FUJI, KL])
    executor = host.executor_for(s)
    picks = {"picks": [{"product_id": "FUJI", "reason": "Full day"}, {"product_id": "KL"}]}
    compare.note_turn(s.state, "what's the difference between the Fuji trip and the KL tour?")
    outcome = await executor.execute("present_products", picks)
    ui = [e.data for e in outcome.events if e.type == "ui"]
    assert ui[0]["component"] == "comparison" and ui[0]["payload"]["entries"][0]["best_for"] == "Full day"
    compare.note_turn(s.state, "show me tours")
    outcome = await executor.execute("present_products", picks)
    assert [e.data["component"] for e in outcome.events if e.type == "ui"] == ["products"]


# 19: no padding a comparison with unrelated products.
def test_prompt_says_no_unrelated_comparator():
    notes = industry_config_overrides()["domain_search_notes"]
    assert "nothing close to compare" in notes and "never pad a comparison" in notes
    assert "never present_products" in notes
