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

from . import compare

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
    # A request for a person is not a product search ("I want to talk to a human").
    "human",
    "real person",
    "talk to",
    "speak to",
    "speak with",
    "contact",
    "support",
    "complain",
    "manager",
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


# Words naming what the store sells (titles, collections, tags), set by the host from the
# catalog index. A message naming one searches even with no cue word: live, "hotel" was
# answered "we don't carry hotels" without a search, with hotel vouchers in the catalog.
VOCABULARY: set[str] = set()
_EXTRA_NOUNS = {"hotel", "hotels", "villa", "stay", "accommodation", "esim", "sim", "tour", "ticket", "book"}


def set_vocabulary(words: set[str]) -> None:
    VOCABULARY.clear()
    VOCABULARY.update(words)


def _names_a_product(text: str) -> bool:
    words = {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"[a-z]+", text.lower())}
    return bool(words & (VOCABULARY | _EXTRA_NOUNS))


def shopping_request(text: str) -> bool:
    # The host's note about button taps is not the shopper's words.
    words = " ".join(line for line in text.splitlines() if not line.startswith("[App events"))
    padded = f" {words.lower().strip()} "
    if len(padded.strip()) < 3 or _SKIP_START.match(words):
        return False
    if any(cue in padded for cue in SKIP_CUES):
        return False
    return any(cue in padded for cue in SHOPPING_CUES) or _names_a_product(words)


# "What do you sell?": answered from the store's collections in the session context
# (current_page.extra.store_sells), not from three ad hoc searches (live: 18.9 s).
OVERVIEW = re.compile(
    r"\bwhat (do|does|else do) (you|the store|this store) (sell|have|carry|offer|stock)\b|"
    r"\bwhat (kind|kinds|sort|sorts|type|types) of (products|things|stuff|items)\b|"
    r"\bwhat('s| is) (in|on) (your|the) (store|shop|catalog)\b",
    re.IGNORECASE,
)

# A change to the cart reads the cart first. Live, "change mug to 3" made no tool call and
# the reply came from what the model believed the cart held.
CART_EDIT = re.compile(
    r"\b(change|update|set|increase|decrease|reduce|bump|lower)\b.*(\bto\b\s*\d|\bqty\b|\bquantity\b|\d)|"
    r"\bmake (it|that|them|those|the \w+) \d+|\b(remove|delete|take out|drop)\b|"
    r"\bonly (want|need) \d+|\b\d+ instead\b",
    re.IGNORECASE,
)


def _cart_edit(config: Any, text: str, state: ShoppingSessionState) -> dict[str, Any] | None:
    return {} if CART_EDIT.search(text) else None


CART_EDIT_RULE = GroundingRule("cart_edit", "get_cart", _cart_edit)


# Sessions whose next message came from a tapped suggestion chip (keyed by state object):
# a chip is a step toward products, so it searches even when its wording has no cue.
_chip_turns: set[int] = set()


def chip_tapped(state: ShoppingSessionState) -> None:
    _chip_turns.add(id(state))


def is_cart_or_signoff(text: str) -> bool:
    """A chip or message that acts on the cart or closes the chat, not a product search."""
    return _skip(text)


def _skip(text: str) -> bool:
    padded = f" {text.lower().strip()} "
    return bool(_SKIP_START.match(text)) or any(cue in padded for cue in SKIP_CUES)


# A tapped chip is a step toward products and always searches, except one that answers the
# assistant's question or confirms a choice: live, "Keep the 7-day plan anyway" forced a
# search the model ran with the query "placeholder".
_ANSWER_CHIP = re.compile(
    r"^\s*(keep|stick|stay|go with|continue|skip|never ?mind|leave|that'?s fine|fine|"
    r"i'?ll (take|keep|go)|use (this|that|the same))\b",
    re.IGNORECASE,
)


def _discovery(config: Any, text: str, state: ShoppingSessionState) -> dict[str, Any] | None:
    # "Compare these two" names nothing new: a forced search would find a different pair
    # (live, it compared the wrong products). The model compares what it last showed.
    if compare.refers_to_shown(text) or OVERVIEW.search(text):
        _chip_turns.discard(id(state))
        return None
    if id(state) in _chip_turns:
        _chip_turns.discard(id(state))
        if _ANSWER_CHIP.match(text):
            return None  # an answer, even with a cue word in it ("keep the 7-day plan")
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

STARTER_CHIPS = ["Show me your best picks", "Plan a 2-day trip", "Gift ideas", "Shop eSIMs"]


def set_starters(collections: list[str]) -> None:
    """Opening chips from the store's own collections (largest first). Live, the opening
    chips offered "electronics" and "home goods" on a travel, eSIM, and books store."""
    if collections:
        STARTER_CHIPS[:] = [f"Shop {title}" for title in collections[:4]]


def product_records(component: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The product records a rendered component showed, in display order."""
    records: list[dict[str, Any]] = []
    for list_key, inner in _PRODUCT_PATHS.get(component, []):
        for entry in payload.get(list_key) or []:
            if not isinstance(entry, dict):
                continue
            found = entry.get(inner) if inner else entry
            for record in found if isinstance(found, list) else [found]:
                if isinstance(record, dict) and record.get("title"):
                    records.append(record)
    return records


def product_titles(component: str, payload: dict[str, Any]) -> list[str]:
    """Titles of the products a rendered component showed, in display order."""
    return [record["title"] for record in product_records(component, payload)]


def _short(title: str) -> str:
    head = re.split(r"\s[–—-]\s|\(", title)[0].strip()
    words = head.split()
    return " ".join(words[:4]) if len(words) > 4 else head


def fallback_chips(titles: list[str], limit: int = 4) -> list[str]:
    """Chips for a reply that ended without any: next steps from the products it showed,
    then general discovery. Every one leads to a product search when tapped."""
    seen = list(dict.fromkeys(_short(t) for t in titles if t))
    chips: list[str] = []
    if seen:
        chips.append(f"Show more like {seen[0]}")
    for starter in STARTER_CHIPS:
        if len(chips) >= limit:
            break
        chips.append(starter)
    return chips[:limit]


def fallback_products(state: ShoppingSessionState, limit: int = 4) -> dict[str, Any] | None:
    """Cards for a tapped chip whose reply showed no products: the products this session
    found most recently. Live, chips the model offered ("Show more Singapore tours", "Show
    jewelry") found nothing new and the reply was text only, a dead end after a tap."""
    recent = [p for p in reversed(state.seen_products.values()) if not p.variant_of][:limit]
    if not recent:
        return None
    return {
        "title": "Closest matches",
        "layout": "carousel",
        "items": [{"product": p.model_dump(exclude_none=True)} for p in recent],
    }


DISCOVERY_RULE = GroundingRule("discovery", "search_products", _discovery)


def install() -> None:
    """Add the rule after the reference rules in the Messages API runtime, which looks
    them up from its module on every turn."""
    from shopping_agent_runtime import orchestrator

    if DISCOVERY_RULE not in orchestrator.GROUNDING_RULES:
        orchestrator.GROUNDING_RULES = (*GROUNDING_RULES, CART_EDIT_RULE, DISCOVERY_RULE)
