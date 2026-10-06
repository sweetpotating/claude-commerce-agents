"""Products before questions, enforced in code: a shopping request's first model round is
pinned to ``search_products``, so the reply is written with real products in hand rather
than opening with a question.

The reference runtime already pins a turn's first round for its grounding rules (a terms
question reads the policies first, an order question the orders). This adds one more rule
after those, so a returns or order question still goes to its own tool first. The model
still chooses the query; the rule only makes sure a search happens.
"""

from __future__ import annotations

import re
from typing import Any

from commerce_common.grounding import GroundingRule
from shopping_agent import ShoppingSessionState
from shopping_agent.grounding import GROUNDING_RULES

# Words that mark a request to find, choose, or plan something.
SHOPPING_CUES = (
    "need",
    "want",
    "looking for",
    "look for",
    "recommend",
    "suggest",
    "idea",
    "gift",
    "present for",
    "plan",
    "trip",
    "travel",
    "itinerary",
    "holiday",
    "vacation",
    "weekend",
    "visit",
    "going to",
    "heading to",
    "flying to",
    "first time",
    "for my",
    "for a ",
    "fun",
    "compare",
    " vs ",
    "versus",
    "best",
    "popular",
    "show me",
    "find",
    "buy",
    "shop",
    "options",
    "which",
    "what should",
    "something",
    "cheap",
    "under ",
    "budget",
    "do you have",
    "do you sell",
    "do you carry",
    "any ",
)

# Requests that act on the cart, memory, or the conversation itself: no search needed.
SKIP_CUES = (
    "cart",
    "checkout",
    "check out",
    "remove",
    "delete",
    "quantity",
    "remember",
    "forget",
    "thank",
    "bye",
    "that's all",
    "that's everything",
)
_SKIP_START = re.compile(r"^\s*(add|yes|yep|yeah|no|nope|ok|okay|sure|great|cool)\b", re.IGNORECASE)


def shopping_request(text: str) -> bool:
    # The host's note about button taps is not the shopper's words.
    words = " ".join(line for line in text.splitlines() if not line.startswith("[App events"))
    padded = f" {words.lower().strip()} "
    if len(padded.strip()) < 3 or _SKIP_START.match(words):
        return False
    if any(cue in padded for cue in SKIP_CUES):
        return False
    return any(cue in padded for cue in SHOPPING_CUES)


# Sessions whose next message came from a tapped suggestion chip (keyed by state object):
# a chip is a step toward products, so it searches even when its wording has no cue.
_chip_turns: set[int] = set()


def chip_tapped(state: ShoppingSessionState) -> None:
    _chip_turns.add(id(state))


def _skip(text: str) -> bool:
    padded = f" {text.lower().strip()} "
    return bool(_SKIP_START.match(text)) or any(cue in padded for cue in SKIP_CUES)


def _discovery(config: Any, text: str, state: ShoppingSessionState) -> dict[str, Any] | None:
    if id(state) in _chip_turns:
        _chip_turns.discard(id(state))
        if not _skip(text):
            return {}
    return {} if shopping_request(text) else None


# -- Suggestion chips on every reply ----------------------------------------------------

# Components whose payload carries products, and where their records sit.
_PRODUCT_PATHS = {
    "products": [("items", "product")],
    "comparison": [("entries", "product")],
    "plan": [("steps", "products")],
    "itinerary": [("days", "products")],
    "guide": [("related_products", None)],
    "plan_matrix": [("plans", None)],
}

STARTER_CHIPS = ("Show me your best picks", "Plan a 2-day trip", "Gift ideas", "Compare your plans")


def product_titles(component: str, payload: dict[str, Any]) -> list[str]:
    """Titles of the products a rendered component showed, in display order."""
    titles: list[str] = []
    for list_key, inner in _PRODUCT_PATHS.get(component, []):
        for entry in payload.get(list_key) or []:
            if not isinstance(entry, dict):
                continue
            found = entry.get(inner) if inner else entry
            for record in found if isinstance(found, list) else [found]:
                if isinstance(record, dict) and record.get("title"):
                    titles.append(record["title"])
    return titles


def _short(title: str) -> str:
    head = re.split(r"\s[–—-]\s|\(", title)[0].strip()
    words = head.split()
    return " ".join(words[:4]) if len(words) > 4 else head


def fallback_chips(titles: list[str], limit: int = 4) -> list[str]:
    """Chips for a reply that ended without any: next steps from the products it showed,
    then general discovery. Every one leads to a product search when tapped."""
    seen = list(dict.fromkeys(_short(t) for t in titles if t))
    chips: list[str] = []
    if len(seen) >= 2:
        chips.append(f"Compare {seen[0]} and {seen[1]}")
    if seen:
        chips.append(f"Show more like {seen[0]}")
    for starter in STARTER_CHIPS:
        if len(chips) >= limit:
            break
        chips.append(starter)
    return chips[:limit]


DISCOVERY_RULE = GroundingRule("discovery", "search_products", _discovery)


def install() -> None:
    """Add the rule after the reference rules in the Messages API runtime, which looks
    them up from its module on every turn."""
    from shopping_agent_runtime import orchestrator

    if DISCOVERY_RULE not in orchestrator.GROUNDING_RULES:
        orchestrator.GROUNDING_RULES = (*GROUNDING_RULES, DISCOVERY_RULE)
