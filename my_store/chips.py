"""Suggestion chips checked against what the store can actually do before they reach the
shopper. Each rule is a failure from the chip-crawl eval:

- No chip for a feature the store does not have ("Notify me when back in stock").
- No chip that names a product the catalog does not have ("Compare the chess set").
- No stale cart chip: "Add X" for an item already in the cart, "Remove X" for one that is
  not, "Check out" with an empty cart.
- While the store's search is failing, no chip that would search: they fail again and again.

Product checks need the catalog's words (``vocabulary``, from the backend's catalog index);
without them only the feature and cart rules apply.
"""

from __future__ import annotations

import re

UNSUPPORTED = re.compile(
    r"\b(notify|notification|alert me|back in stock|restock|wish ?list|price (drop|alert)|"
    r"track (the )?price|subscribe|remind me|email me|text me|sms|save for later|"
    r"pre-?order|wait ?list)\b",
    re.IGNORECASE,
)
_ADD = re.compile(r"^\s*add\s+(?:(?:a|an|the|another|one more|\d+)\s+)?(.+)$", re.IGNORECASE)
_CART_CHANGE = re.compile(r"^\s*(remove|delete|change|update|make)\s+(?:the\s+)?(.+)$", re.IGNORECASE)
_CHECKOUT = re.compile(
    r"^\s*(check ?out|checkout|go to checkout|view (my )?cart|see (my )?cart)\b", re.IGNORECASE
)
_COMPARE = re.compile(r"^\s*compare\s+(.+)$", re.IGNORECASE)
_SPLIT = re.compile(r"\s+(?:and|vs\.?|versus|with|or)\s+|,\s*", re.IGNORECASE)
# "Try the search again", "Retry adding the mug": after a failed call these fail again
# (eval item 45); the reply's own text says when to try again.
RETRY = re.compile(r"\b(retry|try (it |that |this |the \w+ )?again|again)\b", re.IGNORECASE)
_MORE = re.compile(r"\b(another|one more|again|second|extra)\b", re.IGNORECASE)
_FILLER = frozenset(
    "a an the and or of for to in on my your our me show more browse shop see find get some any "
    "all best top new ideas idea picks pick options option other similar like this that these "
    "those under over below above with without from day days trip trips plan plans compare "
    "cheaper cheapest cheap price prices budget sgd usd gift gifts size color colour small medium "
    "large one two".split()
)


def _words(text: str) -> set[str]:
    words = {
        w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
        for w in re.findall(r"[a-z0-9]+", text.lower())
    }
    return {w for w in words if len(w) > 2 and not w.isdigit()}


def _names(text: str, vocabulary: set[str]) -> bool:
    """Every naming word of ``text`` is a word the catalog uses."""
    naming = _words(text) - _FILLER
    return not naming or naming <= vocabulary


def _in(text: str, titles: list[str]) -> bool:
    """``text`` names one of ``titles``: all its naming words appear in one title."""
    naming = _words(text) - _FILLER
    return bool(naming) and any(naming <= _words(t) for t in titles)


def clean(
    chips: list[str],
    *,
    vocabulary: set[str],
    cart_titles: list[str],
    degraded: bool = False,
    after_error: bool = False,
    limit: int = 4,
) -> list[str]:
    out: list[str] = []
    for chip in chips:
        chip = " ".join(str(chip).split())
        if not chip or chip in out or UNSUPPORTED.search(chip):
            continue
        if (after_error or degraded) and RETRY.search(chip):
            continue
        if _CHECKOUT.match(chip):
            if cart_titles:
                out.append(chip)
            continue
        if m := _CART_CHANGE.match(chip):
            if _in(m.group(2), cart_titles):
                out.append(chip)
            continue
        if degraded:
            continue  # every other chip searches or adds from a search
        if m := _ADD.match(chip):
            target = m.group(1)
            if _in(target, cart_titles) and not _MORE.search(chip):
                continue  # already in the cart: stale
            if vocabulary and not _names(target, vocabulary):
                continue
            out.append(chip)
            continue
        if _COMPARE.match(chip) or re.search(r"\b(vs\.?|versus)\b", chip, re.IGNORECASE):
            continue  # the owner's call: no comparison chips (a typed "compare" still works)
        naming = _words(chip) - _FILLER
        if vocabulary and naming and not naming & vocabulary:
            continue  # a search for nothing the store sells
        out.append(chip)
    return out[:limit]


def in_currency(chip: str, currency: str) -> str:
    """'Gifts under $50' -> 'Gifts under SGD 50' in a store that sells in SGD (watch item)."""
    if not currency or currency == "USD":
        return chip
    return re.sub(r"(?:US)?\$\s?(\d)", rf"{currency} \1", chip)


def outage_chips(cart_titles: list[str]) -> list[str]:
    """What still works while search is down: the cart and its checkout."""
    return ["Check out", "View my cart"] if cart_titles else []
