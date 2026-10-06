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
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from commerce_common.streaming import ToolOutcome
from shopping_agent import Product
from shopping_agent.executor import ShoppingToolExecutor

from .shopify_backend import CartRefused, ShopifyError

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

Execute = Callable[[str, dict[str, Any] | None], Awaitable[ToolOutcome]]


class StoreToolExecutor(ShoppingToolExecutor):
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
            outcome = await ShoppingToolExecutor.execute(self, name, tool_input)
            self._remember_cart_lines(outcome)
            return outcome

        return run

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
        return None
