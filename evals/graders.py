"""Code graders: each reads the turns a case produced (the SSE events of /api/chat, plus the
results of card taps) and returns (passed, detail). Only the keys a case names run; the
global checks (no error event, every reply ends with chips) run on every case.

A turn record (runner.py) holds: user, text, tools [(name, input)], results [(name, ok,
summary)], ui [(component, payload)], cart (last cart_update or card-tap cart), error,
secs, usage. A case's checks read the LAST turn unless the key says otherwise
("any_turn_..."), because the earlier turns only set the stage.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

PRODUCT_COMPONENTS = {"products", "comparison", "plan", "itinerary", "plan_matrix", "guide"}
GID = re.compile(r"^gid://shopify/(Product|ProductVariant)/\d+$")
VAGUE_CHIPS = ("tell me more", "anything else", "something else", "more info", "learn more")
CHIPS_TOOL = "present_suggestions"


def products_in(turn: dict) -> list[dict]:
    """Every product record a turn's components showed."""
    out: list[dict] = []
    for component, p in turn["ui"]:
        if component not in PRODUCT_COMPONENTS:
            continue
        out += [i["product"] for i in p.get("items", []) if isinstance(i, dict) and "product" in i]
        for step in p.get("steps") or p.get("days") or []:
            out += step.get("products", [])
        out += p.get("plans", []) + [e["product"] for e in p.get("entries", []) if "product" in e]
        out += [x for x in p.get("related_products", []) if isinstance(x, dict) and "product_id" in x]
    return out


def tool_names(turn: dict) -> list[str]:
    return [name for name, _ in turn["tools"]]


def _lower(values: Any) -> list[str]:
    return [str(v).lower() for v in ([values] if isinstance(values, str) else values)]


def _cart_titles(turn: dict) -> dict[str, int]:
    cart = turn.get("cart") or {}
    return {i["title"].lower(): i["quantity"] for i in cart.get("items", [])}


def _titles_match(wanted: str, titles: dict[str, int]) -> int:
    return sum(q for t, q in titles.items() if wanted in t)


def _checkout(turn: dict) -> dict | None:
    return next((p for c, p in turn["ui"] if c == "checkout"), None)


Check = Callable[[list[dict], Any, dict], tuple[bool, str]]


def _last(turns: list[dict]) -> dict:
    return turns[-1]


def calls_tool(turns, want, case):
    names = tool_names(_last(turns))
    missing = [t for t in want if t not in names]
    return not missing, f"missing {missing}; called {names}"


def calls_one_of(turns, want, case):
    names = tool_names(_last(turns))
    return any(t in names for t in want), f"none of {want}; called {names}"


def never_calls(turns, banned, case):
    called = [t for turn in turns for t in tool_names(turn) if t in banned]
    return not called, f"called {called}"


def first_tool(turns, want, case):
    names = [n for n in tool_names(_last(turns)) if n not in ("load_skill",)]
    first = names[0] if names else None
    ok = first in ([want] if isinstance(want, str) else want)
    return ok, f"first tool {first}, wanted {want}"


def first_tool_not(turns, banned, case):
    names = [n for n in tool_names(_last(turns)) if n not in ("load_skill",)]
    first = names[0] if names else None
    return first not in ([banned] if isinstance(banned, str) else banned), f"first tool {first}"


def ui_components(turns, want, case):
    shown = [c for c, _ in _last(turns)["ui"]]
    missing = [c for c in want if c not in shown]
    return not missing, f"missing {missing}; shown {shown}"


def ui_any(turns, want, case):
    shown = [c for c, _ in _last(turns)["ui"]]
    return any(c in shown for c in want), f"none of {want}; shown {shown}"


def no_ui_components(turns, banned, case):
    shown = [c for c, _ in _last(turns)["ui"] if c in banned]
    return not shown, f"showed {shown}"


def products_shown_min(turns, n, case):
    found = [p for p in products_in(_last(turns)) if GID.match(str(p.get("product_id", "")))]
    return len(found) >= n, f"{len(found)} real products shown, wanted >= {n}"


def products_shown_title(turns, wanted, case):
    titles = [p.get("title", "").lower() for p in products_in(_last(turns))]
    missing = [w for w in _lower(wanted) if not any(w in t for t in titles)]
    return not missing, f"missing {missing}; shown {titles}"


def products_not_shown_title(turns, banned, case):
    titles = [p.get("title", "").lower() for t in turns for p in products_in(t)]
    shown = [b for b in _lower(banned) if any(b in t for t in titles)]
    return not shown, f"showed {shown}"


def cart_contains(turns, wanted, case):
    titles = _cart_titles(_last(turns))
    missing = [w for w in _lower(wanted) if not _titles_match(w, titles)]
    return not missing, f"missing {missing}; cart {titles}"


def cart_not_contains(turns, banned, case):
    titles = _cart_titles(_last(turns))
    present = [b for b in _lower(banned) if _titles_match(b, titles)]
    return not present, f"cart has {present}; cart {titles}"


def cart_quantity(turns, wanted, case):
    titles = _cart_titles(_last(turns))
    wrong = {
        w: _titles_match(w.lower(), titles)
        for w, q in wanted.items()
        if _titles_match(w.lower(), titles) != q
    }
    return not wrong, f"quantities {wrong} != {wanted}"


def cart_item_count(turns, n, case):
    count = sum(_cart_titles(_last(turns)).values())
    return count == n, f"cart has {count} items, wanted {n}"


def checkout_handoff(turns, want, case):
    card = next((c for t in reversed(turns) if (c := _checkout(t))), None)
    urls = [h.get("url", "") for h in (card or {}).get("handoffs", [])]
    ok = any(re.match(r"^https://[\w.-]+\.myshopify\.com/(cart|checkouts)/", u) for u in urls)
    return ok == bool(want), f"handoffs {urls[:1]}"


def reply_includes(turns, wanted, case):
    text = _last(turns)["text"].lower()
    missing = [w for w in _lower(wanted) if w not in text]
    return not missing, f"reply lacks {missing}"


def reply_includes_any(turns, wanted, case):
    text = _last(turns)["text"].lower()
    return any(w in text for w in _lower(wanted)), f"reply has none of {wanted}"


def reply_omits(turns, banned, case):
    text = " ".join(t["text"] for t in turns).lower()
    present = [b for b in _lower(banned) if b in text]
    return not present, f"reply says {present}"


def max_tool_calls(turns, n, case):
    count = len([x for x in tool_names(_last(turns)) if x not in (CHIPS_TOOL, "load_skill")])
    return count <= n, f"{count} tool calls, max {n}"


def max_latency_s(turns, n, case):
    worst = max(t["secs"] for t in turns)
    return worst <= n, f"slowest turn {worst:.1f}s, max {n}s"


GRADERS: dict[str, Check] = {
    f.__name__: f
    for f in (
        calls_tool,
        calls_one_of,
        never_calls,
        first_tool,
        first_tool_not,
        ui_components,
        ui_any,
        no_ui_components,
        products_shown_min,
        products_shown_title,
        products_not_shown_title,
        cart_contains,
        cart_not_contains,
        cart_quantity,
        cart_item_count,
        checkout_handoff,
        reply_includes,
        reply_includes_any,
        reply_omits,
        max_tool_calls,
        max_latency_s,
    )
}


def global_checks(turns: list[dict]) -> dict[str, tuple[bool, str]]:
    """Every case: no error event, and every reply ends on chips with no vague ones."""
    out = {}
    errors = [t["error"] for t in turns if t.get("error")]
    out["no_error"] = (not errors, f"errors {errors}")
    chat_turns = [t for t in turns if t.get("kind", "chat") == "chat"]
    unchipped = [t["user"] for t in chat_turns if not t["ui"] or t["ui"][-1][0] != "suggestions"]
    out["ends_with_chips"] = (not unchipped, f"no chips after {unchipped}")
    vague = [
        chip
        for t in chat_turns
        for c, p in t["ui"]
        if c == "suggestions"
        for chip in p.get("suggestions", [])
        if any(v in chip.lower() for v in VAGUE_CHIPS)
    ]
    out["no_vague_chips"] = (not vague, f"vague chips {vague}")
    return out


def grade(case: dict, turns: list[dict]) -> dict[str, tuple[bool, str]]:
    results = global_checks(turns)
    for key, want in case.get("expected", {}).items():
        if key == "rubric":
            continue
        grader = GRADERS.get(key)
        if grader is None:
            results[key] = (False, f"unknown grader {key!r}")
            continue
        try:
            results[key] = grader(turns, want, case)
        except Exception as error:  # a grader bug is a failed check with its reason
            results[key] = (False, f"grader error: {error!r}")
    return results
