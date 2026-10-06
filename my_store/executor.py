"""The shopping agent's tool executor with this store's cart rules, installed through the
runtime's ``executor_class`` seam (my_store/app.py). Each rule fixes a failure seen live:

- An add in the same round as a search waits for the search. The model once searched for a
  tour and, in the same round, added the mug it had tried before (its id was the only one
  it had); the add is refused, so it adds from the results in its next round.
- Cart errors say what the store said. Any Shopify error used to reach the model as "the
  cart tool is temporarily unavailable", which it told the shopper as "the cart isn't
  working". A refusal of an item the catalog lists as available is not called sold out.
- Cart lines count as seen. A product added by its product id sits in the cart as its
  variant id; that id (in the cart panel and the session context) is now a valid id to add
  again, like the ids search_products and get_product_details return.
- A comparison request is answered with a comparison. Live, "difference between X and Y"
  came back as product cards; present_products of 2-4 picks in a turn that asked to
  compare is shown as present_comparison (compare.py).
- An add that could mean two products asks first. Live, "add 2 of the notebook" with two
  notebooks in the results added one silently; when the shopper's words fit two products
  of this turn's search equally well, the add is refused once with both names.
- No tool error reaches the model empty. Live, a cart failure came back with no text and
  the model guessed "sold out"; timeouts and anything unexpected now say what happened.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from commerce_common.streaming import ToolOutcome
from shopping_agent import Product
from shopping_agent.executor import ShoppingToolExecutor

from . import compare
from .shopify_backend import SEARCH_SOURCE, CartRefused, ShopifyError

SEARCH_FIRST = (
    "Not added: this round also searches the catalog, and its results are not back yet. "
    "Add in your next step, using the product_id the results return for the item the "
    "customer named."
)
CART_REFUSED = (
    "Nothing was added: {detail}. The catalog lists this item as available, so do not call "
    "it sold out. Tell the customer it could not be added just now, and offer to try again "
    "or to contact the store."
)
STORE_ERROR = (
    "The store did not accept that: {detail}. Tell the customer plainly what happened and "
    "offer another step; do not say the cart or a tool is broken."
)

STORE_TIMEOUT = (
    "The store did not answer in time ({detail}). Tell the customer the store is slow right "
    "now and offer to try again in a moment; do not say the item is unavailable."
)
# The catalog could not be checked: never an absence claim, never tool talk (items 36, 40).
SEARCH_FAILED = (
    "The catalog could not be checked just now. Tell the customer, in these words or close: "
    "'I couldn't check the catalog just now - please try again in a moment.' Do not say the "
    "store does not carry or have the item, and do not mention searches, tools, or errors."
)
FROM_INDEX = (
    "\n\nNote: the live catalog search is busy, so these results come from the store's "
    "catalog list. Show them; if none fits, say you couldn't check the full catalog just now "
    "rather than that the store doesn't carry it."
)
AMBIGUOUS_ADD = (
    "Not added yet: '{words}' fits more than one product: {names}. Show them with "
    "present_products and ask the customer which one they mean; add it once they choose."
)

Execute = Callable[[str, dict[str, Any] | None], Awaitable[ToolOutcome]]
logger = logging.getLogger(__name__)
_FENCE = re.compile(r"<storefront_data>\s*(.*?)\s*</storefront_data>", re.S)
_NOT_NAMES = frozenset(
    "add adding want need like please could would some more also them those these that this "
    "with without from into the and for one two three four five six seven eight nine ten "
    "cart basket item items product products piece pieces of can you get buy any all our new "
    "too now pls just".split()
)
# Per chat (keyed by its state object), this turn's words and what its searches found.
_turns: dict[int, dict[str, Any]] = {}


def note_turn(state: Any, text: str) -> None:
    """Called by the host at the start of each turn with the shopper's message."""
    _turns[id(state)] = {"text": text, "found": {}, "asked": False}
    compare.note_turn(state, text)
    if len(_turns) > 5000:  # forget old chats
        for key in list(_turns)[:1000]:
            del _turns[key]


def _words(text: str) -> set[str]:
    """Words that can tell products apart: names, sizes, numbers in titles ('7 Days 10GB'),
    but not a quantity ('add 2 of the notebook')."""
    text = re.sub(r"\b(add|want|get|buy|need|take|order)\s+\d+\b", r"\1", text.lower())
    text = re.sub(r"\b\d+\s*(x|of|pcs|pieces)\b", " ", text)
    words = {
        w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
        for w in re.findall(r"[a-z0-9]+", text)
    }
    return {w for w in words if (len(w) >= 3 or w.isdigit()) and w not in _NOT_NAMES}


class StoreToolExecutor(ShoppingToolExecutor):
    # True for a card's own button (the host's /api/cart/add): the shopper picked the exact
    # product, so nothing is ambiguous.
    direct = False

    @property
    def execute(self) -> Execute:
        """A fresh function per access. The runtime takes ``executor.execute`` once per model
        round (its dispatcher) and each API call takes it once, so the calls one function
        sees are exactly one round's."""
        round_calls: list[str] = []

        async def run(name: str, tool_input: dict[str, Any] | None) -> ToolOutcome:
            if name == "add_to_cart" and "search_products" in round_calls:
                return ToolOutcome.error(SEARCH_FIRST)
            round_calls.append(name)
            if name == "present_products" and (as_table := compare.as_comparison(self._state, tool_input)):
                name, tool_input = compare.TOOL, as_table
            if name == "add_to_cart" and (ask := self._ambiguous(tool_input)):
                return ToolOutcome.error(ask)
            outcome = await ShoppingToolExecutor.execute(self, name, tool_input)
            if name == "search_products":
                source = SEARCH_SOURCE.get()
                if outcome.is_error and source == "failed":
                    outcome = ToolOutcome.error(SEARCH_FAILED)
                elif not outcome.is_error:
                    self._note_found(outcome.result_text)
                    if source == "index":
                        outcome.result_text += FROM_INDEX
            self._remember_cart_lines(outcome)
            return outcome

        return run

    def _note_found(self, result_text: str) -> None:
        turn = _turns.get(id(self._state))
        match = _FENCE.search(result_text or "")
        if turn is None or not match:
            return
        try:
            results = json.loads(match.group(1)).get("results") or []
        except (ValueError, AttributeError):
            return
        for r in results:
            if isinstance(r, dict) and r.get("product_id") and r.get("title"):
                turn["found"][r["product_id"]] = r["title"]

    def _ambiguous(self, tool_input: dict[str, Any] | None) -> str | None:
        """The refusal for an add whose words fit two of this turn's results equally well."""
        turn = _turns.get(id(self._state))
        product_id = (tool_input or {}).get("product_id")
        if self.direct or turn is None or turn["asked"] or not product_id or not turn["found"]:
            return None
        said = _words(turn["text"])
        known = self._state.seen_products.get(product_id)
        chosen = known.variant_of if known is not None and known.variant_of else product_id
        if chosen not in turn["found"]:
            return None
        score = {pid: len(said & _words(title)) for pid, title in turn["found"].items()}
        best = score[chosen]
        ties = [pid for pid, n in score.items() if n == best]
        if best == 0 or len(ties) < 2:
            return None
        turn["asked"] = True
        names = ", ".join(turn["found"][pid] for pid in ties[:4])
        words = " ".join(sorted(said & _words(turn["found"][chosen])))
        return AMBIGUOUS_ADD.format(words=words, names=names)

    def _remember_cart_lines(self, outcome: ToolOutcome) -> None:
        state = self._state
        for event in outcome.events:
            if event.type != "cart_update":
                continue
            cart = event.data.get("cart") or {}
            state.remember_products(
                [
                    Product(
                        product_id=line["product_id"],
                        title=line.get("title") or line["product_id"],
                        price=line.get("price") or 0,
                        currency=cart.get("currency") or "USD",
                        image_url=line.get("image_url"),
                        option_values=line.get("option_values") or {},
                        variant_of=line.get("variant_of"),
                    )
                    for line in cart.get("items") or []
                    if line.get("product_id") and line["product_id"] not in state.seen_products
                ]
            )

    def domain_error(self, error: Exception) -> ToolOutcome | None:
        detail = self._sanitize(str(error), 200)
        if isinstance(error, CartRefused):
            return ToolOutcome.error(CART_REFUSED.format(detail=detail or "the cart refused it"))
        if (outcome := super().domain_error(error)) is not None:
            return outcome
        if isinstance(error, ShopifyError):
            return ToolOutcome.error(STORE_ERROR.format(detail=detail or "an error"))
        if isinstance(error, httpx.HTTPStatusError):
            return ToolOutcome.error(
                STORE_ERROR.format(detail=f"the store answered {error.response.status_code}")
            )
        if isinstance(error, httpx.TransportError):
            return ToolOutcome.error(STORE_TIMEOUT.format(detail=type(error).__name__))
        # Never an empty error: the model fills a blank with a guess ("sold out").
        logger.warning("tool failed: %s", error, exc_info=True)
        return ToolOutcome.error(STORE_ERROR.format(detail=detail or type(error).__name__))
