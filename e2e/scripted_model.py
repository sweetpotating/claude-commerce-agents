"""A scripted stand-in for the Claude client, so the whole bot can be driven end to end -
real runtime, gates, host, live Shopify store, chat page - without one API call.

Each shopper message is matched against SCENARIOS (a regex -> a policy). A policy is a
function of the turn so far: it reads the live tool results already returned in this turn
(search results, product details, cart) and returns the next model step - tool calls or
the closing text. So the ids it adds or compares are the store's real ids, found the way
the model would find them, and every tool, gate, enrichment, and card runs for real.

What this proves is the plumbing a shopper depends on; what the model would choose is the
evals' job (evals/, which do call the API).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from commerce_common.testing import FakeStream, create_response, text_block, text_message, tool_calls_message

FENCE = re.compile(r"<storefront_data>\s*(.*?)\s*</storefront_data>", re.S)


class Turn:
    """What a policy sees: the shopper's message and this turn's tool results so far."""

    def __init__(self, messages: list[dict], system: Any = None, tool_choice: Any = None) -> None:
        self.user = ""
        self.forced = (tool_choice or {}).get("name") if isinstance(tool_choice, dict) else None
        self.context = _session_context(system)  # the host's per-turn session context
        self.results: list[tuple[str, str]] = []  # (tool name, result text), this turn only
        names: dict[str, str] = {}
        start = 0
        for i, m in enumerate(messages):
            content = m.get("content")
            if m.get("role") == "user" and (
                isinstance(content, str)
                or any(isinstance(b, dict) and b.get("type") == "text" for b in content or [])
            ):
                start = i
        for m in messages[start:]:
            content = m.get("content")
            if m.get("role") == "user":
                if isinstance(content, str):
                    self.user = content
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "text" and not b["text"].startswith("[App"):
                        self.user = b["text"]
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        text = b.get("content")
                        if isinstance(text, list):
                            text = " ".join(x.get("text", "") for x in text if isinstance(x, dict))
                        self.results.append((names.get(b.get("tool_use_id"), "?"), str(text)))
            for b in content if isinstance(content, list) else []:
                kind = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
                if kind == "tool_use":
                    bid = b["id"] if isinstance(b, dict) else b.id
                    names[bid] = b["name"] if isinstance(b, dict) else b.name

    def called(self, tool: str) -> bool:
        return any(n == tool for n, _ in self.results)

    def data(self, tool: str) -> Any:
        """The last result of ``tool`` this turn, parsed from its data fence."""
        for name, text in reversed(self.results):
            if name == tool and (m := FENCE.search(text)):
                return json.loads(m.group(1))
        return None

    def products(self, tool: str = "search_products") -> list[dict]:
        found = self.data(tool) or {}
        return found.get("results", []) if isinstance(found, dict) else []

    def find(self, word: str, tool: str = "search_products") -> dict | None:
        return next((p for p in self.products(tool) if word.lower() in p["title"].lower()), None)


Policy = Callable[[Turn], Any]


def _session_context(system: Any) -> dict:
    blocks = system if isinstance(system, list) else [{"text": system or ""}]
    for block in blocks:
        text = block.get("text", "") if isinstance(block, dict) else ""
        if "# Session context" in text and (m := FENCE.search(text)):
            return json.loads(m.group(1))
    return {}


def page_extra(t: Turn) -> dict:
    return (t.context.get("current_page") or {}).get("extra") or {}


def chips(*labels: str) -> tuple[str, dict]:
    return ("present_suggestions", {"suggestions": list(labels)})


# -- policies, one per shopper intent -------------------------------------------------------


def discovery(t: Turn):
    if not t.called("search_products"):
        return tool_calls_message(("search_products", {"query": "gift"}))
    picks = [{"product_id": p["product_id"], "reason": "A thoughtful pick"} for p in t.products()[:3]]
    return tool_calls_message(
        ("present_products", {"picks": picks}), chips("Gift ideas under 30", "Show more gifts")
    )


def tee_sizes(t: Turn):
    if not t.called("search_products"):
        return tool_calls_message(("search_products", {"query": "tee"}))
    tee = t.find("tee")
    if not t.called("get_product_details"):
        return tool_calls_message(("get_product_details", {"product_id": tee["product_id"]}))
    sizes = ", ".join(
        v["option_values"].get("Size", "") for v in (t.data("get_product_details") or {}).get("variants", [])
    )
    return tool_calls_message(
        ("present_products", {"picks": [{"product_id": tee["product_id"], "reason": f"Sizes {sizes}"}]}),
        chips("Add size M", "Show similar tees"),
    )


def add_tee_m(t: Turn):
    if not t.called("search_products"):
        return tool_calls_message(("search_products", {"query": "tee"}))
    if not t.called("get_product_details"):
        return tool_calls_message(("get_product_details", {"product_id": t.find("tee")["product_id"]}))
    if not t.called("add_to_cart"):
        variant = next(
            v for v in t.data("get_product_details")["variants"] if v["option_values"].get("Size") == "M"
        )
        return tool_calls_message(("add_to_cart", {"product_id": variant["product_id"]}))
    return tool_calls_message(chips("Check out", "Add a Japan eSIM"))


def add_by_name(word: str, query: str, qty: int = 1):
    def policy(t: Turn):
        if not t.called("search_products"):
            return tool_calls_message(("search_products", {"query": query}))
        if not t.called("add_to_cart"):
            return tool_calls_message(
                ("add_to_cart", {"product_id": t.find(word)["product_id"], "quantity": qty})
            )
        return tool_calls_message(chips("Check out", "Show more"))

    return policy


def cart_edit(tool: str, word: str, qty: int | None = None):
    def policy(t: Turn):
        if not t.called("get_cart"):
            return tool_calls_message(("get_cart", {}))
        if not t.called(tool):
            line = next(i for i in t.data("get_cart")["items"] if word.lower() in i["title"].lower())
            args = {"product_id": line["product_id"]} | ({"quantity": qty} if qty is not None else {})
            return tool_calls_message((tool, args))
        return tool_calls_message(chips("Check out", "Show more"))

    return policy


def compare_places(t: Turn):
    if not t.called("search_products"):
        return tool_calls_message(
            ("search_products", {"query": "Japan"}), ("search_products", {"query": "Malaysia"})
        )
    found = [
        p
        for _, text in t.results
        if (m := FENCE.search(text))
        for p in json.loads(m.group(1)).get("results", [])
    ]
    fuji = next(p for p in found if "fuji" in p["title"].lower())
    kl = next(p for p in found if "kuala lumpur" in p["title"].lower())
    entries = [  # with pros and cons, as the real model sends them
        {"product_id": fuji["product_id"], "best_for": "A full day with lunch", "pros": ["Lunch included"]},
        {"product_id": kl["product_id"], "best_for": "A half-day city taster", "cons": ["No meal"]},
    ]
    return tool_calls_message(
        ("present_comparison", {"title": "Mt Fuji vs Kuala Lumpur", "entries": entries}),
        chips(f"Add {fuji['title']}", f"Add {kl['title']}"),
    )


def compare_shown(t: Turn):
    """'Compare these two': the two products the last reply showed, no search, no text (the
    host adds the summary line)."""
    ids = [p["product_id"] for p in page_extra(t).get("last_shown", [])[:2]]
    return tool_calls_message(
        ("present_comparison", {"entries": [{"product_id": i} for i in ids]}), chips("Show more gifts")
    )


def difference(t: Turn):
    """'Difference between X and Y' answered with present_products, as the model did live:
    the host shows it as a comparison."""
    if not t.called("search_products"):
        return tool_calls_message(
            ("search_products", {"query": "Fuji"}), ("search_products", {"query": "Kuala Lumpur"})
        )
    found = [
        p
        for _, text in t.results
        if (m := FENCE.search(text))
        for p in json.loads(m.group(1)).get("results", [])
    ]
    fuji = next(p for p in found if "fuji" in p["title"].lower())
    kl = next(p for p in found if "kuala lumpur" in p["title"].lower())
    return tool_calls_message(
        (
            "present_products",
            {"picks": [{"product_id": fuji["product_id"]}, {"product_id": kl["product_id"]}]},
        ),
        chips(f"Add {kl['title']}"),
    )


def compare_this_with_mug(t: Turn):
    """'Compare this with the mug' on a product page: 'this' is the page's product."""
    viewing = page_extra(t).get("viewing") or {}
    if not t.called("search_products"):
        return tool_calls_message(("search_products", {"query": "mug"}))
    mug = t.find("mug")
    entries = [{"product_id": viewing["product_id"]}, {"product_id": mug["product_id"]}]
    return tool_calls_message(
        ("present_comparison", {"entries": entries, "dimensions": ["Price", "Delivery"]}),
        chips(f"Add {mug['title']}"),
    )


def compare_ghost(t: Turn):
    """A third id never returned by a tool: the call is refused, then retried grounded."""
    if not t.called("search_products"):
        return tool_calls_message(("search_products", {"query": "streaming"}))
    found = [p["product_id"] for p in t.products()[:2]]
    tries = [n for n, _ in t.results if n == "present_comparison"]
    if not tries:
        ids = [*found, "gid://shopify/Product/999999"]
        return tool_calls_message(("present_comparison", {"entries": [{"product_id": i} for i in ids]}))
    if len(tries) == 1:
        return tool_calls_message(
            ("present_comparison", {"entries": [{"product_id": i} for i in found]}),
            chips("Show more streaming cards"),
        )
    return text_message("Those two side by side.")


def say_then(text: str, *calls: tuple) -> Any:
    """Text, then tool calls, in one message (the model often writes a line first)."""
    message = tool_calls_message(*calls)
    message.content.insert(0, text_block(text))
    return message


def most_expensive(t: Turn):
    if not t.called("search_products"):
        return tool_calls_message(("search_products", {"query": "most expensive"}))
    picks = [{"product_id": p["product_id"]} for p in t.products()[:3]]
    return tool_calls_message(("present_products", {"picks": picks}), chips("Show the cheapest"))


def hotels(t: Turn):
    if not t.called("search_products"):
        return tool_calls_message(("search_products", {"query": "hotel"}))
    picks = [{"product_id": p["product_id"]} for p in t.products()[:4]]
    return tool_calls_message(("present_products", {"picks": picks}), chips("Show Tokyo tours"))


LOOKING = "Let me look through the catalog for that for you."


def repeats_itself(t: Turn):
    """Text before the search, then the same line again after it, as seen live."""
    if not t.called("search_products"):
        return say_then(LOOKING, ("search_products", {"query": "mug"}))
    mug = t.find("mug")
    return say_then(
        f"{LOOKING} Here is the mug.",
        ("present_products", {"picks": [{"product_id": mug["product_id"]}]}),
        chips(
            "Notify me when back in stock",
            "Compare the drone and the mug",
            "Show more mugs",
            "Wishlist it",
        ),
    )


def change_quantity(t: Turn):
    """'Change the mug to 3': the host forces get_cart first; this checks it did."""
    if not t.called("get_cart"):
        if t.forced != "get_cart":
            return text_message("(get_cart was not forced for a cart change)")
        return tool_calls_message(("get_cart", {}))
    if not t.called("update_cart_item"):
        line = next(i for i in t.data("get_cart")["items"] if "mug" in i["title"].lower())
        return tool_calls_message(("update_cart_item", {"product_id": line["product_id"], "quantity": 3}))
    return text_message("Done: 3 mugs.")


def compare_plans(t: Turn):
    if not t.called("search_products"):
        return tool_calls_message(("search_products", {"query": "plan"}))
    plans = [p for p in t.products() if "postpaid" in p["title"].lower()]
    ids = [p["product_id"] for p in plans[:2]]
    return tool_calls_message(
        (
            "present_plan_comparison",
            {"title": "Mobile plans", "plan_ids": ids, "recommended_plan_id": ids[0]},
        ),
        chips("Add the Starter plan", "Add the Plus plan"),
    )


def itinerary(t: Turn):
    if not t.called("search_products"):
        return tool_calls_message(("search_products", {"query": "Singapore"}))
    found = t.products()
    pick = lambda w: [p["product_id"] for p in found if w in p["title"].lower()][:1]  # noqa: E731
    days = [
        {
            "label": "Day 1: Arrive",
            "note": "Airport transfer and a SIM",
            "product_ids": pick("transfer") + pick("sim"),
        },
        {"label": "Day 2: Sights", "note": "City pass day", "product_ids": pick("city pass")},
    ]
    return tool_calls_message(
        ("present_itinerary", {"title": "Two days in Singapore", "days": days}), chips("Add the City Pass")
    )


def policy_answer(t: Turn):
    if not t.called("search_policies"):
        return tool_calls_message(("search_policies", {"query": t.user}))
    found = t.data("search_policies") or []
    first = (
        found[0]["content"]
        if isinstance(found, list) and found
        else "the store's policies say nothing on that"
    )
    return text_message(f"Per the store's policy: {first}")


def handoff(t: Turn):
    if not t.called("search_policies"):
        return tool_calls_message(("search_policies", {"query": "How do I contact customer support?"}))
    return text_message("A person at the store can help: email tanyueting96@gmail.com.")


def checkout(t: Turn):
    if not t.called("checkout"):
        return tool_calls_message(("checkout", {}))
    return text_message("Tap Check out securely to finish on the store's checkout.")


def unseen_add(t: Turn):
    if not t.called("add_to_cart"):
        return tool_calls_message(("add_to_cart", {"product_id": "gid://shopify/ProductVariant/1"}))
    return text_message("I can only add products I've shown you.")


def remember(t: Turn):
    if not t.called("save_memory"):
        return tool_calls_message(
            ("save_memory", {"key": "diet", "value": "vegetarian", "category": "preference"})
        )
    return text_message("Noted.")


SCENARIOS: list[tuple[str, Policy]] = [
    (r"\bgifts?\b|^shop ", discovery),
    (r"what sizes", tee_sizes),
    (r"add the (logo )?tee in (size )?m", add_tee_m),
    (r"add (a |the )?(bookworm )?mug", add_by_name("mug", "mug")),
    (r"20 universal", add_by_name("universal studios", "Universal Studios", 20)),
    (r"add the japan esim", add_by_name("japan esim", "Japan eSIM")),
    (r"make it 2 mugs", cart_edit("update_cart_item", "mug", 2)),
    (r"remove the esim", cart_edit("remove_from_cart", "esim")),
    (r"most expensive", most_expensive),
    (r"hotels", hotels),
    (r"mug again", repeats_itself),
    (r"change the mug to 3", change_quantity),
    (r"compare (these|them)", compare_shown),
    (r"difference between", difference),
    (r"compare this with the mug", compare_this_with_mug),
    (r"compare three streaming cards", compare_ghost),
    (r"compare .*japan.*malaysia|fuji vs", compare_places),
    (r"compare .*plans", compare_plans),
    (r"itinerary|plan a .*trip", itinerary),
    (r"return|refund|exchange|ship", policy_answer),
    (r"human|contact|support", handoff),
    (r"check ?out", checkout),
    (r"free item hack", unseen_add),
    (r"remember", remember),
]


class ScriptedClient:
    """messages.stream plays the matching policy; messages.create (background memory
    extraction) answers that there is nothing to store."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.messages = self

    outage = False  # True: every model call fails as an out-of-credit account does

    def stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        if self.outage:
            import anthropic
            import httpx

            request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.BadRequestError(
                "Your credit balance is too low to access the Anthropic API.",
                response=httpx.Response(400, request=request),
                body=None,
            )
        turn = Turn(kwargs["messages"], kwargs.get("system"), kwargs.get("tool_choice"))
        for pattern, policy in SCENARIOS:
            if re.search(pattern, turn.user, re.IGNORECASE):
                step = policy(turn)
                if step is not None:
                    return FakeStream(step)
        return FakeStream(text_message(f"(no script for: {turn.user[:60]})"))

    async def create(self, **kwargs: Any):
        return create_response(text_block("nothing to remember"), stop_reason="end_turn")
