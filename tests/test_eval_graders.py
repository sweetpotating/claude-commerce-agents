"""The eval graders, offline: an oracle transcript passes every check it should and a null
one fails, so a wiring bug in the harness shows here before it costs a paid run."""

from __future__ import annotations

import json

from evals.graders import grade
from evals.runner import parse_sse, turn_record

TEE = {"product_id": "gid://shopify/Product/9061072011454", "title": "iKnowledge Logo Tee", "price": 29.9}
CART = {"items": [{"title": "iKnowledge Logo Tee - M", "quantity": 2}], "subtotal": 59.8, "currency": "SGD"}
CHECKOUT = {"cart": CART, "handoffs": [{"url": "https://shop.myshopify.com/cart/c/abc?key=1"}]}

EVENTS = [
    ("tool_call", {"tool": "search_products", "input": {"query": "tee"}}),
    ("tool_result", {"tool": "search_products", "is_error": False, "summary": "1 result"}),
    ("ui", {"component": "products", "payload": {"items": [{"product": TEE}]}}),
    ("tool_call", {"tool": "add_to_cart", "input": {"product_id": "x"}}),
    ("cart_update", {"cart": CART}),
    ("ui", {"component": "checkout", "payload": CHECKOUT}),
    ("text_delta", {"text": "Added 2 tees. Returns within 30 days."}),
    ("ui", {"component": "suggestions", "payload": {"suggestions": ["Show similar tees"]}}),
    ("turn_complete", {"usage": {"input_tokens": 10, "output_tokens": 5}}),
]
SSE = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in EVENTS) + ": keep-alive\n\n"

EXPECTED = {
    "first_tool": "search_products",
    "calls_tool": ["add_to_cart"],
    "never_calls": ["save_memory"],
    "ui_components": ["products", "checkout"],
    "products_shown_min": 1,
    "products_shown_title": ["logo tee"],
    "cart_contains": ["logo tee - m"],
    "cart_quantity": {"logo tee": 2},
    "cart_item_count": 2,
    "checkout_handoff": True,
    "reply_includes": ["30 days"],
    "reply_omits": ["sold out"],
    "max_tool_calls": 2,
}


def test_an_oracle_turn_passes_every_check():
    turn = turn_record("add a tee", parse_sse(SSE), 1.0)
    assert turn["usage"] == {"input_tokens": 10, "output_tokens": 5}
    results = grade({"expected": EXPECTED}, [turn])
    assert all(ok for ok, _ in results.values()), {k: v for k, v in results.items() if not v[0]}


def test_a_null_turn_fails_the_checks_that_matter():
    empty = turn_record("add a tee", [], 1.0)
    results = grade({"expected": EXPECTED}, [empty])
    for key in (
        "first_tool",
        "calls_tool",
        "products_shown_min",
        "cart_contains",
        "checkout_handoff",
        "ends_with_chips",
    ):
        assert not results[key][0], key
    assert grade({"expected": {"no_such_grader": 1}}, [empty])["no_such_grader"][0] is False
