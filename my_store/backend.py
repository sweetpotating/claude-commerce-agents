"""MyStoreBackend: the one integration surface you implement to connect your store.

Every method below maps to one of your systems. This file ships backed by
``catalog.json`` so it runs out of the box; each method has a ``TODO(live)`` naming what to
call instead (your search API, cart API, OMS, CMS, ...). The model never sees credentials
or URLs: methods run server-side and the agent reads only what they return (fenced).

Contract: ``vendor/commerce-agents/shopping-agent/core/shopping_agent/backend.py``.
Mapping guide: ``vendor/commerce-agents/docs/backends.md``.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from shopping_agent import (
    Cart,
    CartItem,
    CheckoutHandoff,
    FulfillmentOption,
    Order,
    Policy,
    Product,
    ProductDetails,
    SearchFilters,
    ShoppingSessionContext,
    StorefrontBackend,
    Unavailable,
    UserPreferences,
)

CATALOG_PATH = Path(__file__).with_name("catalog.json")
# Where your hosted checkout lives (Shopify/commercetools/Stripe Checkout/your own route).
CHECKOUT_BASE_URL = os.environ.get("STORE_CHECKOUT_BASE_URL", "https://checkout.example.com/c")

_DETAIL_ONLY = ("long_description", "specs", "review_highlights", "variants", "stock")


class MyStoreBackend(StorefrontBackend):
    def __init__(self, catalog_path: Path = CATALOG_PATH) -> None:
        data = json.loads(catalog_path.read_text())
        self._raw = {p["product_id"]: p for p in data["products"]}
        self._policies = [Policy(**p) for p in data["policies"]]
        self._orders = {user: [Order(**o) for o in orders] for user, orders in data["orders"].items()}
        self._users = data["users"]
        self._variants: dict[str, Product] = {}
        for raw in self._raw.values():
            for variant in self._variant_records(raw):
                self._variants[variant.product_id] = variant
        # TODO(live): carts live in your cart service, keyed by the customer's cart token.
        self._carts: dict[str, Cart] = {}
        # Checkout tokens your hosted checkout resolves back to a cart.
        self.checkout_tokens: dict[str, tuple[str, str]] = {}

    # -- mapping helpers ---------------------------------------------------------------

    def _variant_records(self, raw: dict[str, Any]) -> list[Product]:
        base = {k: v for k, v in raw.items() if k not in (*_DETAIL_ONLY, "options")}
        return [
            Product(
                **(
                    base
                    | {
                        "product_id": v["product_id"],
                        "price": v["price"],
                        "in_stock": v["stock"] > 0,
                        "option_values": v["option_values"],
                        "variant_of": raw["product_id"],
                    }
                )
            )
            for v in raw.get("variants", [])
        ]

    def _summary(self, raw: dict[str, Any]) -> Product:
        """A search result: a family shows its lowest in-stock price and is in stock while
        any variant is (docs/backends.md, Step 4)."""
        fields = {k: v for k, v in raw.items() if k not in _DETAIL_ONLY}
        variants = raw.get("variants")
        if variants:
            in_stock = [v for v in variants if v["stock"] > 0]
            fields["in_stock"] = bool(in_stock)
            fields["price"] = min(v["price"] for v in (in_stock or variants))
        else:
            fields["in_stock"] = raw.get("stock", 0) > 0
        return Product(**fields)

    def _purchasable(self, product_id: str) -> Product | None:
        if product_id in self._variants:
            return self._variants[product_id]
        raw = self._raw.get(product_id)
        return self._summary(raw) if raw and not raw.get("options") else None

    # -- Catalog -----------------------------------------------------------------------

    async def search_products(
        self,
        session: ShoppingSessionContext,
        query: str,
        filters: SearchFilters | None = None,
        limit: int = 8,
    ) -> list[Product]:
        # TODO(live): call your search engine (Algolia, Elastic, Vertex Search, platform
        # search API) and map hits through _summary-equivalent logic.
        filters = filters or SearchFilters()
        words = [w for w in query.lower().split() if len(w) > 2]
        hits = []
        for raw in self._raw.values():
            product = self._summary(raw)
            haystack = " ".join(
                [product.title, product.category or "", product.short_description or ""]
                + list(product.attributes.values())
            ).lower()
            score = sum(w in haystack for w in words)
            if words and score == 0:
                continue
            if filters.category and product.category != filters.category:
                continue
            if filters.max_price is not None and product.price > filters.max_price:
                continue
            if filters.min_price is not None and product.price < filters.min_price:
                continue
            if filters.min_rating is not None and (product.rating or 0) < filters.min_rating:
                continue
            if any(product.attributes.get(k) != v for k, v in filters.attributes.items()):
                continue
            hits.append((score, product))
        hits.sort(key=lambda h: -h[0])
        results = [p for _, p in hits]
        if filters.sort == "price_asc":
            results.sort(key=lambda p: p.price)
        elif filters.sort == "price_desc":
            results.sort(key=lambda p: -p.price)
        elif filters.sort == "rating":
            results.sort(key=lambda p: -(p.rating or 0))
        return results[:limit]

    async def get_product_details(
        self, session: ShoppingSessionContext, product_id: str
    ) -> ProductDetails | None:
        # TODO(live): your PDP/product API.
        if product_id in self._variants:
            return ProductDetails(**self._variants[product_id].model_dump())
        raw = self._raw.get(product_id)
        if raw is None:
            return None
        summary = self._summary(raw).model_dump()
        return ProductDetails(
            **summary,
            long_description=raw.get("long_description"),
            specs=raw.get("specs", {}),
            review_highlights=raw.get("review_highlights", []),
            variants=self._variant_records(raw),
        )

    # -- Cart (the only writes; already provenance-gated and capped by the executor) ---

    async def get_cart(self, session: ShoppingSessionContext) -> Cart:
        return self._carts.setdefault(session.session_id, Cart())

    async def add_to_cart(self, session: ShoppingSessionContext, product_id: str, quantity: int) -> Cart:
        # TODO(live): POST to your cart API; enforce stock/eligibility there atomically.
        product = self._purchasable(product_id)
        if product is None:
            raise Unavailable(f"{product_id} is not sold on its own.")
        if not product.in_stock:
            siblings = [
                v.product_id
                for v in self._variants.values()
                if v.variant_of and v.variant_of == product.variant_of and v.in_stock
            ]
            raise Unavailable(
                f"{product_id} is out of stock" + (f"; in stock: {', '.join(siblings)}" if siblings else "")
            )
        cart = await self.get_cart(session)
        for item in cart.items:
            if item.product_id == product_id:
                item.quantity += quantity
                return cart
        cart.items.append(
            CartItem(
                product_id=product.product_id,
                title=product.title,
                price=product.price,
                quantity=quantity,
                option_values=product.option_values,
                variant_of=product.variant_of,
            )
        )
        return cart

    async def update_cart_item(self, session: ShoppingSessionContext, product_id: str, quantity: int) -> Cart:
        cart = await self.get_cart(session)
        for item in cart.items:
            if item.product_id == product_id:
                item.quantity = quantity
        return cart

    async def remove_from_cart(self, session: ShoppingSessionContext, product_id: str) -> Cart:
        cart = await self.get_cart(session)
        cart.items = [i for i in cart.items if i.product_id != product_id]
        return cart

    # -- Checkout handoff: where the money moves (never through the model) -------------

    async def checkout_handoff(self, session: ShoppingSessionContext, cart: Cart) -> list[CheckoutHandoff]:
        # TODO(live): create a checkout session on your platform (e.g. a Stripe Checkout
        # Session, a Shopify cart's checkoutUrl, commercetools order-from-cart) and return
        # its URL. Derive an idempotency key from session_id + cart lines.
        token = uuid.uuid4().hex[:12]
        self.checkout_tokens[token] = (session.session_id, session.user_id)
        return [CheckoutHandoff(url=f"{CHECKOUT_BASE_URL}/{token}", label="Pay securely")]

    # -- Customer context ----------------------------------------------------------------

    async def get_preferences(self, session: ShoppingSessionContext) -> UserPreferences:
        # TODO(live): your profile/CRM service; return a guest profile for anonymous users.
        raw = self._users.get(session.user_id)
        return UserPreferences(**raw) if raw else UserPreferences(user_id=session.user_id)

    # -- Orders and policies ---------------------------------------------------------------

    async def get_orders(self, session: ShoppingSessionContext, limit: int = 5) -> list[Order]:
        # TODO(live): your OMS, scoped to session.user_id only.
        orders = self._orders.get(session.user_id, [])
        return sorted(orders, key=lambda o: o.placed_at, reverse=True)[:limit]

    async def get_order(self, session: ShoppingSessionContext, order_id: str) -> Order | None:
        return next((o for o in self._orders.get(session.user_id, []) if o.order_id == order_id), None)

    async def search_policies(self, session: ShoppingSessionContext, query: str) -> list[Policy]:
        # TODO(live): your CMS / help-center search.
        words = [w for w in query.lower().split() if len(w) > 2]
        return [
            p
            for p in self._policies
            if any(w in f"{p.title} {p.category} {p.content}".lower() for w in words)
        ]

    async def get_fulfillment_options(
        self, session: ShoppingSessionContext, product_ids: list[str]
    ) -> list[FulfillmentOption]:
        # TODO(live): your shipping-rate / store-pickup service.
        if not any(self._purchasable(pid) or pid in self._raw for pid in product_ids):
            return []
        return [
            FulfillmentOption(method="shipping", eta="3-5 business days", fee=0.0),
            FulfillmentOption(method="delivery", eta="1-2 business days", fee=15.0),
        ]

    # -- Called by your host when payment completes on the hosted checkout -------------

    def record_paid_order(self, token: str) -> Order | None:
        session_id, user_id = self.checkout_tokens.pop(token, (None, None))
        cart = self._carts.pop(session_id, None) if session_id else None
        if cart is None or not cart.items:
            return None
        order = Order(
            order_id=f"ORD-{uuid.uuid4().hex[:6].upper()}",
            status="processing",
            placed_at=datetime.now(),
            items=[
                {"product_id": i.product_id, "title": i.title, "quantity": i.quantity, "price": i.price}
                for i in cart.items
            ],
            total=cart.subtotal,
        )
        self._orders.setdefault(user_id, []).append(order)
        return order
