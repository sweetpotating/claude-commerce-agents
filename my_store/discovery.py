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


def _discovery(config: Any, text: str, _: ShoppingSessionState) -> dict[str, Any] | None:
    return {} if shopping_request(text) else None


DISCOVERY_RULE = GroundingRule("discovery", "search_products", _discovery)


def install() -> None:
    """Add the rule after the reference rules in the Messages API runtime, which looks
    them up from its module on every turn."""
    from shopping_agent_runtime import orchestrator

    if DISCOVERY_RULE not in orchestrator.GROUNDING_RULES:
        orchestrator.GROUNDING_RULES = (*GROUNDING_RULES, DISCOVERY_RULE)
