"""Keep selling when the model is unavailable (out of API credit, rate-limited, overloaded).

A failed model turn used to end on "the assistant is unavailable". This module answers the
same message without the model, from the store's own tools:

- A policy question (returns, shipping, exchanges, contact...) gets the best-matching FAQ
  answer, quoted as the store wrote it.
- Anything else is searched in the catalog, keyword by keyword (Shopify's search matches
  every word, so a whole sentence finds nothing), and the products come back as cards with
  their Add to cart buttons; those buttons, the cart, and checkout never needed the model.
- On a product page, the page's product is shown when nothing else matches.

No wording is generated: every sentence here is fixed text or the store's own data, so a
degraded reply cannot invent a fact.
"""

from __future__ import annotations

import re
from typing import Any

from shopping_agent import PageContext, Product, ShoppingSessionContext, ShoppingSessionState

# Words that carry no product meaning in a shopper's message.
_STOP = frozenset(
    """a an and any are as at be can could do does for from get give got have help hi hello hey
    how i i'd i'm id im in is it its just like looking may me my need of on or please pls show
    so some something the there this to want wanted what whats what's when where which who
    with would you your yours we our us find buy get got let lets let's tell about more other
    good best nice great cool new one ones thing things stuff kind sort type some any also
    really very much many lot lots under over around than then them they their it's that
    sell sells selling sold carry stock have has love loves loved like likes into enjoy enjoys
    mum mom mother dad father wife husband friend friends boyfriend girlfriend son daughter
    kid kids someone somebody him her his hers gift gifts present ideas idea
    cheap cheapest affordable budget price prices cost costs""".split()
)
_POLICY_WORDS = (
    "return",
    "refund",
    "exchange",
    "ship",
    "deliver",
    "shipping",
    "policy",
    "policies",
    "contact",
    "support",
    "email",
    "human",
    "warranty",
    "cancel",
    "track",
    "order status",
    "pickup",
    "payment",
    "pay with",
    "store credit",
    "refundable",
)
MAX_KEYWORDS = 4
MAX_PRODUCTS = 8


def keywords(message: str) -> list[str]:
    """The message's product words, most specific first (longer words tend to be nouns)."""
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", message.lower())
    kept = list(dict.fromkeys(w for w in words if w not in _STOP))
    return sorted(kept, key=len, reverse=True)[:MAX_KEYWORDS]


def is_policy_question(message: str) -> bool:
    text = message.lower()
    return any(w in text for w in _POLICY_WORDS)


async def answer(
    backend: Any,
    ctx: ShoppingSessionContext,
    state: ShoppingSessionState,
    message: str,
    page: PageContext | None,
    contact_email: str,
) -> tuple[str, dict | None, list[str]]:
    """(text, products payload or None, chips) for a turn the model could not take."""
    lead = "Our assistant is unavailable right now, so here is what the store's own pages say"
    if is_policy_question(message):
        try:
            found = await backend.search_policies(ctx, message)
        except Exception:
            found = []
        if found:
            best = found[0]
            text = f"{lead}.\n\n**{best.title}**\n{best.content}\n\nFor anything else, email {contact_email}."
            return text, None, ["Show me your best picks", "Gift ideas"]

    products: dict[str, Product] = {}
    matched: list[str] = []
    for word in keywords(message) or ["gift"]:
        # Shopify matches whole words: "tours" finds nothing where "tour" finds tours.
        for term in dict.fromkeys([word, word[:-1] if word.endswith("s") and len(word) > 3 else word]):
            try:
                found = await backend.search_products(ctx, term, None, MAX_PRODUCTS)
            except Exception:
                found = []
            for p in found:
                products.setdefault(p.product_id, p)
            if found:
                matched.append(term)
                break
        if len(products) >= MAX_PRODUCTS:
            break
    if not products and page is not None and page.product_id:
        try:
            details = await backend.get_product_details(ctx, page.product_id)
        except Exception:
            details = None
        if details is not None:
            state.remember_products([details, *details.variants])
            products[details.product_id] = details
    if not products:
        try:  # nothing matched the words: a browse of the catalog keeps the shopper moving
            for p in await backend.search_products(ctx, "", None, MAX_PRODUCTS):
                products.setdefault(p.product_id, p)
        except Exception:
            pass
    if not products:
        text = (
            "Our assistant is unavailable right now. Your cart is saved and checkout still works, "
            f"or email {contact_email} for help."
        )
        return text, None, []

    picks = list(products.values())[:MAX_PRODUCTS]
    state.remember_products(picks)  # so their Add to cart buttons pass the provenance gate
    tail = (
        "Tap Add to cart on any of them; your cart and checkout work as usual. "
        f"For help, email {contact_email}."
    )
    asked = [w for w in keywords(message) if w not in matched and w.rstrip("s") not in matched]
    if matched:
        found = ", ".join(f"'{w}'" for w in matched)
        missing = f" Nothing matched {', '.join(repr(w) for w in asked)}." if asked else ""
        text = (
            f"Our assistant is unavailable right now, but here are products matching {found}.{missing} {tail}"
        )
    else:
        what = ", ".join(repr(w) for w in asked) or "that"
        text = (
            f"Our assistant is unavailable right now, and the store has nothing matching {what}. "
            f"Here are some of its products instead. {tail}"
        )
    payload = {
        "title": "Matching products",
        "layout": "carousel",
        "items": [{"product": p.model_dump(exclude_none=True)} for p in picks],
    }
    chips = [f"Show more {k}" for k in matched[:2]] + ["Show me your best picks", "Gift ideas"]
    return text, payload, chips[:4]
