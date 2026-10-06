"""ShopifyUCPBackend: the shopping agent's StorefrontBackend over one Shopify store's UCP tools.

| Backend method            | Shopify tool (endpoint)                                         |
|---------------------------|-----------------------------------------------------------------|
| search_products           | search_catalog            (https://{shop}/api/ucp/mcp)          |
| get_product_details       | get_product, one call per option combination as needed          |
| get/add/update/remove cart| create_cart, get_cart, update_cart (PUT), cancel_cart           |
| checkout_handoff          | create_checkout -> continue_url when credentials are set,       |
|                           | otherwise the cart's own continue_url                           |
| search_policies           | search_shop_policies_and_faqs  (https://{shop}/api/mcp)         |
| get_preferences           | guest profile (Shopify exposes no buyer profile here)           |

Order history and delivery options have no Shopify tool on this path: Order MCP only returns
orders the agent itself completed, and delivery options are resolved inside checkout. Build
the agent with ``shopify_agent_config()``, which switches those systems off.

Settings (environment): SHOPIFY_STORE_DOMAIN (required), UCP_AGENT_PROFILE_URL (your hosted
agent profile), SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET (Dev Dashboard catalog API key,
needed for Checkout MCP), SHOPIFY_BUYER_COUNTRY (e.g. US), CHECKOUT_UTM_SOURCE.

Docs: https://shopify.dev/docs/agents
"""

from __future__ import annotations

import asyncio
import html
import itertools
import json
import os
import re
import time
import uuid
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
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
    ShoppingAgentConfig,
    ShoppingSessionContext,
    StorefrontBackend,
    Unavailable,
    UserPreferences,
)

# Shopify's hosted example profile declares catalog, cart, checkout and order capabilities.
# Fine for development; host your own before going live (shopify.dev/docs/agents/profiles).
EXAMPLE_PROFILE = "https://shopify.dev/ucp/agent-profiles/examples/2026-08-25/valid-with-capabilities.json"
TOKEN_URL = "https://api.shopify.com/auth/access_token"

# Variant lookups per product family; the reference caps a family's details at ~60 rows.
MAX_VARIANT_FETCHES = 30
_ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "ISK", "UGX", "XAF", "XOF"}
_UNAVAILABLE_CODES = ("out_of_stock", "insufficient_stock", "unavailable", "not_available", "sold_out")
_TAG = re.compile(r"<[^>]+>")


class ShopifyError(RuntimeError):
    """A Shopify call failed; the executor reports the tool as temporarily unavailable."""


def _money(value: Any, currency: str = "USD") -> float:
    """UCP amounts are integers in minor units ({"amount": 8900} is 89.00 USD)."""
    if isinstance(value, dict):
        currency = value.get("currency", currency)
        value = value.get("amount")
    if value is None:
        return 0.0
    return round(float(value) / (1 if currency in _ZERO_DECIMAL else 100), 2)


def _minor(amount: float) -> int:
    return int(round(amount * 100))


def _text(description: Any) -> str | None:
    if isinstance(description, dict):
        description = description.get("plain") or description.get("html")
    if not description:
        return None
    return " ".join(html.unescape(_TAG.sub(" ", str(description))).split()) or None


class ShopifyUCPBackend(StorefrontBackend):
    def __init__(
        self,
        shop_domain: str,
        *,
        agent_profile_url: str = EXAMPLE_PROFILE,
        client_id: str | None = None,
        client_secret: str | None = None,
        buyer_country: str | None = None,
        utm_source: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        domain = shop_domain.removeprefix("https://").removeprefix("http://").strip("/")
        self.ucp_endpoint = f"https://{domain}/api/ucp/mcp"
        self.mcp_endpoint = f"https://{domain}/api/mcp"
        self.profile = agent_profile_url
        self._client_id, self._client_secret = client_id, client_secret
        self._country = buyer_country
        self._utm_source = utm_source
        self._http = http or httpx.AsyncClient(timeout=20)
        self._ids = itertools.count(1)
        self._token: tuple[str, float] | None = None
        # TODO(live): keep these in your session store so a restart keeps carts.
        self._cart_ids: dict[str, str] = {}
        self._continue_urls: dict[str, str] = {}
        self._buyer_ips: dict[str, str] = {}
        # What the store told us, so cart lines can carry titles and option values.
        self._variants: dict[str, Product] = {}
        self._default_variant: dict[str, str] = {}

    @classmethod
    def from_env(cls) -> ShopifyUCPBackend:
        return cls(
            os.environ["SHOPIFY_STORE_DOMAIN"],
            agent_profile_url=os.environ.get("UCP_AGENT_PROFILE_URL", EXAMPLE_PROFILE),
            client_id=os.environ.get("SHOPIFY_CLIENT_ID"),
            client_secret=os.environ.get("SHOPIFY_CLIENT_SECRET"),
            buyer_country=os.environ.get("SHOPIFY_BUYER_COUNTRY"),
            utm_source=os.environ.get("CHECKOUT_UTM_SOURCE"),
        )

    # -- transport -----------------------------------------------------------------------

    async def _access_token(self) -> str:
        if not (self._client_id and self._client_secret):
            raise ShopifyError("SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET are not set")
        if self._token and self._token[1] > time.monotonic():
            return self._token[0]
        response = await self._http.post(
            TOKEN_URL,
            json={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "client_credentials",
            },
        )
        response.raise_for_status()
        token = response.json()["access_token"]
        self._token = (token, time.monotonic() + 55 * 60)  # tokens last 60 minutes
        return token

    async def _call(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        ucp: bool = True,
        auth: bool = False,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if ucp:
            meta = {"ucp-agent": {"profile": self.profile}, **arguments.get("meta", {})}
            arguments = {**arguments, "meta": meta}
        headers = {"Content-Type": "application/json", **(headers or {})}
        if auth:
            headers["Authorization"] = f"Bearer {await self._access_token()}"
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": next(self._ids),
            "params": {"name": tool, "arguments": arguments},
        }
        response = await self._http.post(
            self.ucp_endpoint if ucp else self.mcp_endpoint, json=body, headers=headers
        )
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise ShopifyError(f"{tool}: {data['error']}")
        result = data.get("result") or {}
        content = result.get("structuredContent")
        if content is None:
            texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
            joined = "\n".join(texts)
            try:
                content = json.loads(joined)
            except ValueError:
                content = {"text": joined}
        if result.get("isError"):
            messages = content.get("messages") if isinstance(content, dict) else None
            raise ShopifyError(f"{tool}: {messages or content}")
        return content

    def _context(self) -> dict[str, Any]:
        return {"address_country": self._country} if self._country else {}

    # -- mapping -------------------------------------------------------------------------

    @staticmethod
    def _options(raw: dict[str, Any]) -> dict[str, list[str]]:
        options = {
            o["name"]: [v["label"] for v in o.get("values", []) if v.get("exists", True)]
            for o in raw.get("options") or []
        }
        # Shopify gives single-variant products one "Title: Default Title" option.
        if len(options) == 1 and list(options.values())[0] in (["Default Title"], []):
            return {}
        return {name: labels for name, labels in options.items() if labels}

    def _product(self, raw: dict[str, Any]) -> Product:
        price_range = raw.get("price_range") or {}
        low = price_range.get("min") or (raw.get("variants") or [{}])[0].get("price") or {}
        variants = raw.get("variants") or []
        available = [v for v in variants if (v.get("availability") or {}).get("available", True)]
        media = raw.get("media") or []
        options = self._options(raw)
        product = Product(
            product_id=raw["id"],
            title=raw.get("title", raw["id"]),
            brand=raw.get("vendor") or ((variants[0].get("seller") or {}).get("name") if variants else None),
            price=_money(low),
            currency=low.get("currency", "USD") if isinstance(low, dict) else "USD",
            image_url=media[0].get("url") if media and isinstance(media[0], dict) else None,
            in_stock=bool(available) if variants else True,
            short_description=(_text(raw.get("description")) or "")[:300] or None,
            options=options,
        )
        if not options and len(variants) == 1:
            self._default_variant[product.product_id] = variants[0]["id"]
        return product

    def _variant(self, raw: dict[str, Any], family: Product) -> Product:
        values = {o["name"]: o["label"] for o in raw.get("options") or []}
        variant = family.model_copy(
            update={
                "product_id": raw["id"],
                "price": _money(raw.get("price"), family.currency),
                "in_stock": (raw.get("availability") or {}).get("available", True),
                "options": {},
                "option_values": values,
                "variant_of": family.product_id,
            }
        )
        self._variants[variant.product_id] = variant
        return variant

    # -- Catalog -------------------------------------------------------------------------

    async def search_products(
        self,
        session: ShoppingSessionContext,
        query: str,
        filters: SearchFilters | None = None,
        limit: int = 8,
    ) -> list[Product]:
        filters = filters or SearchFilters()
        catalog: dict[str, Any] = {"query": query, "pagination": {"limit": limit}}
        if self._context():
            catalog["context"] = self._context()
        price = {
            key: _minor(value)
            for key, value in (("min", filters.min_price), ("max", filters.max_price))
            if value is not None
        }
        if price:
            catalog["filters"] = {"price": price}
        content = await self._call("search_catalog", {"catalog": catalog})
        products = [self._product(raw) for raw in content.get("products", [])]
        if filters.min_rating is not None:
            products = [p for p in products if p.rating is None or p.rating >= filters.min_rating]
        if filters.sort == "price_asc":
            products.sort(key=lambda p: p.price)
        elif filters.sort == "price_desc":
            products.sort(key=lambda p: -p.price)
        return products[:limit]

    async def _get_product(
        self, product_id: str, selected: list[dict[str, str]] | None = None
    ) -> dict[str, Any] | None:
        catalog: dict[str, Any] = {"id": product_id}
        if selected:
            catalog["selected"] = selected
        if self._context():
            catalog["context"] = self._context()
        content = await self._call("get_product", {"catalog": catalog})
        return content.get("product")

    async def get_product_details(
        self, session: ShoppingSessionContext, product_id: str
    ) -> ProductDetails | None:
        raw = await self._get_product(product_id)
        if raw is None:
            return None
        family = self._product(raw)
        details = {
            **family.model_dump(),
            "long_description": _text(raw.get("description")),
        }
        variants_raw = {v["id"]: v for v in raw.get("variants") or []}

        if "ProductVariant" in product_id and product_id in variants_raw:
            # A variant id asked for directly comes back as that variant.
            variant = self._variant(variants_raw[product_id], family)
            return ProductDetails(**{**details, **variant.model_dump()})
        if not family.options:
            return ProductDetails(**details)

        # get_product returns the variants matching a selection, so walk the option
        # combinations it has not returned yet, up to a cap.
        names = list(family.options)
        seen = {tuple((o["name"], o["label"]) for o in v.get("options") or []) for v in variants_raw.values()}
        missing = [
            combo
            for combo in itertools.product(*family.options.values())
            if tuple(zip(names, combo, strict=True)) not in seen
        ][:MAX_VARIANT_FETCHES]

        async def fetch(combo: tuple[str, ...]) -> list[dict[str, Any]]:
            selected = [{"name": n, "label": label} for n, label in zip(names, combo, strict=True)]
            product = await self._get_product(family.product_id, selected)
            wanted = {(s["name"], s["label"]) for s in selected}
            return [
                v
                for v in (product or {}).get("variants") or []
                if {(o["name"], o["label"]) for o in v.get("options") or []} == wanted
            ]

        for found in await asyncio.gather(*(fetch(c) for c in missing)):
            variants_raw.update({v["id"]: v for v in found})
        details["variants"] = [self._variant(v, family) for v in variants_raw.values()]
        return ProductDetails(**details)

    # -- Cart ------------------------------------------------------------------------------

    def _cart(self, session: ShoppingSessionContext, raw: dict[str, Any]) -> Cart:
        if raw.get("continue_url"):
            self._continue_urls[session.session_id] = raw["continue_url"]
        currency = raw.get("currency", "USD")
        items = []
        for line in raw.get("line_items") or []:
            item = line.get("item") or {}
            known = self._variants.get(item.get("id", ""))
            price = item.get("price")
            if price is None:
                subtotal = next((t for t in line.get("totals") or [] if t.get("type") == "subtotal"), None)
                price = (subtotal or {}).get("amount", 0) / max(line.get("quantity", 1), 1)
            items.append(
                CartItem(
                    product_id=item.get("id", ""),
                    title=item.get("title") or (known.title if known else item.get("id", "")),
                    price=_money(price, currency),
                    quantity=line.get("quantity", 1),
                    image_url=item.get("image_url") or (known.image_url if known else None),
                    option_values=known.option_values if known else {},
                    variant_of=known.variant_of if known else None,
                )
            )
        return Cart(items=items, currency=currency)

    @staticmethod
    def _raise_if_unavailable(raw: dict[str, Any], product_id: str) -> None:
        for message in raw.get("messages") or []:
            code = str(message.get("code", "")).lower()
            if message.get("type") == "error" and any(c in code for c in _UNAVAILABLE_CODES):
                raise Unavailable(f"{product_id} is not available right now ({code})")

    async def _raw_cart(self, session: ShoppingSessionContext) -> dict[str, Any] | None:
        cart_id = self._cart_ids.get(session.session_id)
        if not cart_id:
            return None
        content = await self._call("get_cart", {"id": cart_id})
        cart = content.get("cart") or content
        if any(m.get("code") == "not_found" for m in cart.get("messages") or []):
            self._cart_ids.pop(session.session_id, None)  # expired; start over next add
            return None
        return cart

    async def _put_lines(
        self,
        session: ShoppingSessionContext,
        lines: dict[str, int],
        changed: str,
        variant_id: str | None = None,
    ) -> Cart:
        """Write the whole cart (update_cart replaces everything it is sent)."""
        cart_id = self._cart_ids.get(session.session_id)
        payload: dict[str, Any] = {
            "line_items": [{"quantity": q, "item": {"id": vid}} for vid, q in lines.items()]
        }
        if self._context():
            payload["context"] = self._context()
        if not lines:
            if cart_id:
                meta = {"idempotency-key": str(uuid.uuid4())}
                await self._call("cancel_cart", {"id": cart_id, "meta": meta})
                self._cart_ids.pop(session.session_id, None)
            return Cart()
        if cart_id:
            content = await self._call("update_cart", {"id": cart_id, "cart": payload})
        else:
            content = await self._call("create_cart", {"cart": payload})
        raw = content.get("cart") or content
        self._cart_ids[session.session_id] = raw.get("id", cart_id)
        self._raise_if_unavailable(raw, changed)
        # Shopify can also drop a line with only a warning (e.g. merchandise_out_of_stock when
        # inventory is 0), so check that the line written is actually in the returned cart.
        variant_id = variant_id or changed
        if lines.get(variant_id) and variant_id not in {
            (line.get("item") or {}).get("id") for line in raw.get("line_items") or []
        }:
            reasons = "; ".join(m.get("content") or m.get("code", "") for m in raw.get("messages") or [])
            detail = f" ({reasons})" if reasons else ""
            raise Unavailable(f"{changed} could not be added to the cart{detail}")
        return self._cart(session, raw)

    async def _lines(self, session: ShoppingSessionContext) -> dict[str, int]:
        raw = await self._raw_cart(session)
        return {
            (line.get("item") or {}).get("id"): line.get("quantity", 1)
            for line in (raw or {}).get("line_items") or []
        }

    async def get_cart(self, session: ShoppingSessionContext) -> Cart:
        raw = await self._raw_cart(session)
        return self._cart(session, raw) if raw else Cart()

    async def _variant_id(self, product_id: str) -> str:
        if "ProductVariant" in product_id:
            return product_id
        if product_id not in self._default_variant:
            raw = await self._get_product(product_id)
            variants = (raw or {}).get("variants") or []
            if not variants:
                raise Unavailable(f"{product_id} has no purchasable variant")
            self._default_variant[product_id] = variants[0]["id"]
        return self._default_variant[product_id]

    async def add_to_cart(self, session: ShoppingSessionContext, product_id: str, quantity: int) -> Cart:
        known = self._variants.get(product_id)
        if known is not None and not known.in_stock:
            siblings = [
                v.product_id
                for v in self._variants.values()
                if v.variant_of == known.variant_of and v.in_stock
            ]
            raise Unavailable(
                f"{product_id} is out of stock" + (f"; in stock: {', '.join(siblings)}" if siblings else "")
            )
        variant_id = await self._variant_id(product_id)
        lines = await self._lines(session)
        lines[variant_id] = lines.get(variant_id, 0) + quantity
        return await self._put_lines(session, lines, product_id, variant_id)

    async def update_cart_item(self, session: ShoppingSessionContext, product_id: str, quantity: int) -> Cart:
        lines = await self._lines(session)
        if product_id not in lines:
            return await self.get_cart(session)
        lines[product_id] = quantity
        return await self._put_lines(session, lines, product_id)

    async def remove_from_cart(self, session: ShoppingSessionContext, product_id: str) -> Cart:
        lines = await self._lines(session)
        if lines.pop(product_id, None) is None:
            return await self.get_cart(session)
        return await self._put_lines(session, lines, product_id)

    # -- Checkout: hand off to Shopify's own checkout; the agent never takes payment -------

    def _attributed(self, url: str) -> str:
        if not self._utm_source:
            return url
        parts = urlsplit(url)
        query = f"{parts.query}&" if parts.query else ""
        return urlunsplit(parts._replace(query=query + urlencode({"utm_source": self._utm_source})))

    def set_buyer_ip(self, session_id: str, ip: str | None) -> None:
        """Record the shopper's IP; authenticated checkout sends it to Shopify for bot checks."""
        if ip:
            self._buyer_ips[session_id] = ip

    async def checkout_handoff(self, session: ShoppingSessionContext, cart: Cart) -> list[CheckoutHandoff]:
        cart_id = self._cart_ids.get(session.session_id)
        if not cart_id:
            return []
        url = None
        buyer_ip = self._buyer_ips.get(session.session_id)
        if self._client_id and self._client_secret and buyer_ip:
            # create_checkout takes line items, not a cart id, and refuses a request without
            # the buyer's IP ("Missing required buyer IP header"). The checkout comes back
            # "incomplete" until the buyer gives contact details on Shopify's page.
            lines = await self._lines(session)
            checkout_args = {
                "checkout": {
                    "line_items": [{"quantity": q, "item": {"id": vid}} for vid, q in lines.items()]
                },
                "meta": {"idempotency-key": str(uuid.uuid4())},
            }
            try:
                content = await self._call(
                    "create_checkout",
                    checkout_args,
                    auth=True,
                    headers={"Shopify-Storefront-Buyer-IP": buyer_ip},
                )
                checkout = content.get("checkout") or content
                url = checkout.get("continue_url")
            except (httpx.HTTPError, ShopifyError):
                url = None  # the cart's own link below still reaches Shopify checkout
        url = url or self._continue_urls.get(session.session_id)
        if not url:
            return []
        return [CheckoutHandoff(url=self._attributed(url), label="Check out securely")]

    # -- Customer context, orders, policies, fulfillment -------------------------------------

    async def get_preferences(self, session: ShoppingSessionContext) -> UserPreferences:
        # TODO(live): read your own profile store, or Customer Accounts MCP after buyer sign-in.
        return UserPreferences(user_id=session.user_id)

    async def get_orders(self, session: ShoppingSessionContext, limit: int = 5) -> list[Order]:
        return []  # switched off by shopify_agent_config(); see the module docstring

    async def get_order(self, session: ShoppingSessionContext, order_id: str) -> Order | None:
        return None

    async def search_policies(self, session: ShoppingSessionContext, query: str) -> list[Policy]:
        content = await self._call("search_shop_policies_and_faqs", {"query": query}, ucp=False)
        # Live stores answer with a JSON list of {"question", "answer"} pairs.
        entries = content if isinstance(content, list) else content.get("policies") or content.get("results")
        if isinstance(entries, list):
            return [
                Policy(
                    policy_id=str(e.get("id") or e.get("title") or e.get("question") or i),
                    title=e.get("title") or e.get("question") or "Store policy",
                    content=_text(e.get("body") or e.get("content") or e.get("answer")) or "",
                )
                for i, e in enumerate(entries)
                if isinstance(e, dict)
            ]
        answer = content.get("text") or content.get("answer") or json.dumps(content)
        if not answer.strip():
            return []
        return [Policy(policy_id="shopify-policies-faqs", title="Store policies and FAQs", content=answer)]

    async def get_fulfillment_options(
        self, session: ShoppingSessionContext, product_ids: list[str]
    ) -> list[FulfillmentOption]:
        return []  # switched off; Shopify resolves delivery options inside checkout


def shopify_agent_config(**overrides: Any) -> ShoppingAgentConfig:
    """Agent config for this backend: order history and delivery options off (no tool for
    them on this path), Shopify ids recognized when a customer pastes one."""
    defaults: dict[str, Any] = {
        "brand_name": os.environ.get("STORE_BRAND_NAME", "our store"),
        "enable_orders": False,
        "enable_fulfillment": False,
        # Without this the model assumed a generic "store" sells only physical goods and
        # turned down a tour the live catalog carries, without searching.
        "domain_search_notes": os.environ.get(
            "STORE_SEARCH_NOTES",
            "The catalog can hold services, experiences, and digital vouchers as well as "
            "physical goods; search before saying the store does not carry something.",
        ),
        "product_id_patterns": (
            *ShoppingAgentConfig.model_fields["product_id_patterns"].default,
            r"gid://shopify/(?:Product|ProductVariant|p)/[\w-]+",
        ),
    }
    return ShoppingAgentConfig(**(defaults | overrides))
