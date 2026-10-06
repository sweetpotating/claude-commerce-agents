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
needed for Checkout MCP), SHOPIFY_BUYER_COUNTRY, CHECKOUT_UTM_SOURCE.

Set SHOPIFY_BUYER_COUNTRY to a country one of the store's markets ships to (SG for the
iKnowledge store). Without it Shopify places the buyer by the server's IP; a country with no
shipping makes every physical product come back from the cart as "already sold out", even
though search reports it available. Items that need no shipping are unaffected.

Docs: https://shopify.dev/docs/agents
"""

from __future__ import annotations

import asyncio
import copy
import html
import itertools
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from pathlib import Path
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

from .facts import extract
from .places import PLACE_NAMES, expansions

logger = logging.getLogger(__name__)

# Shopify's hosted example profile declares catalog, cart, checkout and order capabilities.
# Fine for development; host your own before going live (shopify.dev/docs/agents/profiles).
EXAMPLE_PROFILE = "https://shopify.dev/ucp/agent-profiles/examples/2026-08-25/valid-with-capabilities.json"
TOKEN_URL = "https://api.shopify.com/auth/access_token"

# Variant lookups per product family; the reference caps a family's details at ~60 rows.
MAX_VARIANT_FETCHES = 30
_RETRIES = 5  # cart and checkout writes, for 429, 430 and 502-504: 0.5+1+2+4+8s at most
# Catalog reads retry less, and after Shopify throttles this server they stop for a while
# (answered from the catalog index): live, after a test crawl, every search retried for
# 15s, the retries kept the throttle going, and replies hung until the connection dropped.
_READ_TOOLS = {"search_catalog", "get_product", "lookup_catalog", "search_shop_policies_and_faqs"}
_READ_RETRIES = 2
_COOLDOWN_SECONDS = 60.0
_MAX_COOLDOWN_SECONDS = 300.0
# The catalog as last read on a machine that could reach the store (scripts/catalog_snapshot.py),
# shipped with the code: what the index starts from if the live read fails at boot.
SNAPSHOT_PATH = Path(__file__).parent / "catalog_snapshot.json"
# 429: rate limited. 430: Shopify's bot protection ("Security Rejection"), which a burst of
# requests from one server IP can trip. 502-504: briefly down.
_RETRY_STATUSES = (429, 430, 502, 503, 504)
# The whole catalog, read page by page with an empty query and kept for an hour: what the
# store sells by collection, catalog-wide price ranking (search returns a relevance cut of
# at most 25), checking chip targets, and search answers while Shopify's search is failing.
_INDEX_SECONDS = 3600
_INDEX_PAGE = 50
_INDEX_MAX_PAGES = 20
# The store is "degraded" after this many failed catalog calls within the window with no
# success since: chips stop offering searches that would fail again.
_DEGRADED_FAILURES = 3
_DEGRADED_WINDOW = 120.0
# Words for a price ranking. Search is a relevance cut, so "most expensive" over a search
# found the S$129 record player, not the S$480 villa: these sort the whole catalog instead.
_SUPERLATIVE = re.compile(
    r"\b(most[- ]expensive|priciest|highest[- ]priced|dearest|most[- ]premium|luxury|luxurious|"
    r"cheapest|least[- ]expensive|lowest[- ]priced|most[- ]affordable|cheap|budget|inexpensive)\b",
    re.IGNORECASE,
)
_ASCENDING = re.compile(r"cheap|least|lowest|affordable|budget|inexpensive", re.IGNORECASE)
# Words that say nothing about which product ("what is your most expensive item?").
_GENERIC_WORDS = frozenset(
    "item items product products thing things stuff one ones option options store shop catalog "
    "sell sells selling carry have has got available something anything show find get buy "
    "whats what's most least price priced prices expensive cost costs".split()
)
# A shopper's word -> the words the catalog titles use for the same thing. Live: "hotel"
# was answered "we don't carry hotels" with hotel vouchers and a villa stay in the catalog.
_SEARCH_SYNONYMS: dict[str, tuple[str, ...]] = {
    "hotel": ("hotel", "villa", "stay"),
    "accommodation": ("hotel", "villa", "stay"),
    "lodging": ("hotel", "villa", "stay"),
    "stay": ("hotel", "villa", "stay"),
    "villa": ("villa", "hotel"),
    "resort": ("hotel", "villa"),
    "room": ("hotel", "villa"),
    "sim": ("esim", "sim"),
    "esim": ("esim", "sim"),
    "data": ("esim",),
    "luggage": ("suitcase", "packing"),
    "suitcase": ("suitcase",),
    "show": ("ticket", "show", "concert"),
    "tickets": ("ticket",),
}
# Catalog reads repeat within a chat and across chats (the same search, the same product's
# variants); a short cache keeps a busy hour under Shopify's rate limit. Cart and checkout
# calls are never cached.
_CACHED_TOOLS = {"search_catalog", "get_product", "search_shop_policies_and_faqs"}
_CACHE_SECONDS = 120
_CACHE_SIZE = 1000
_FRESH_CART_SECONDS = 30
_ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "ISK", "UGX", "XAF", "XOF"}
_UNAVAILABLE_CODES = ("out_of_stock", "insufficient_stock", "unavailable", "not_available", "sold_out")
_TAG = re.compile(r"<[^>]+>")
# Live descriptions join paragraphs with no tag or space ("with lunch.Experience with ...").
_RUN_ON = re.compile(r"(?<=[a-z)\]])([.!?])(?=[A-Z])")
# search_shop_policies_and_faqs answers only questions close to the store's own FAQ wording
# and returns [] otherwise (live: "service fees", "ticket transfer", even "refund"). These
# phrasings reach the store's general policies, which often settle the question anyway.
_FALLBACK_POLICY_QUERIES = ("What is your return policy?", "What is your shipping policy?")
# Phrasings that reach a typical store's FAQ entries. Their answers seed a local FAQ list
# that search_policies also matches by keyword, because Shopify's own search finds an entry
# only from near its exact wording (live: "how do I contact support" found the contact FAQ;
# "contact", "contact support" and "exchange" found nothing).
_FAQ_SEED_QUERIES = (
    *_FALLBACK_POLICY_QUERIES,
    "How do I contact customer support?",
    "Can I refund or exchange a product?",
    "Do you accept store credit?",
    "What countries do you ship to?",
    "What payment methods do you accept?",
    "How do I track my order?",
    "Can I cancel or change my order?",
    "Do you offer gift wrapping?",
)
_FAQ_TTL_SECONDS = 3600
_STOPWORDS = frozenset(
    "a an and are can do does for how i in is it me my of on or the to what when where which "
    "who why will with you your we our there any".split()
)
# Words a shopper uses for the same FAQ topic; each group counts as one keyword.
_SYNONYMS = (
    {
        "contact",
        "support",
        "email",
        "e-mail",
        "phone",
        "call",
        "reach",
        "human",
        "person",
        "agent",
        "someone",
        "talk",
        "speak",
    },
    {"exchange", "swap", "replace", "replacement"},
    {"refund", "return", "returns", "money"},
    {"ship", "shipping", "deliver", "delivery", "send", "post", "country", "countries"},
    {"cancel", "cancellation", "change", "modify"},
    {"pay", "payment", "card", "credit", "paypal"},
    {"track", "tracking", "where", "status"},
)
# Words that ask for a ranking the catalog has no data for (no sales or rating counts).
_RANKING_WORDS = re.compile(
    r"\b(best[\s-]?sell(?:er|ers|ing)?|top[\s-]?rated|popular|trending|top|best|hot|favou?rites?)\b",
    re.IGNORECASE,
)
# What this backend needs the model to know about Shopify, added to the system prompt.
SHOPIFY_PROMPT_NOTES = (
    "The store's search matches every word of a query, so a long query often finds nothing: "
    "search one or two key words (e.g. 'Singapore', 'tour', 'plan'). When a two-word query "
    "finds nothing, search its key word alone ('phone plan' -> 'plan') before switching to a "
    "neighbouring category or concluding the store does not carry something. "
    "Search results are a relevance cut and can leave out matching items (live, a search for "
    "'esim' sometimes omits the Thailand eSIM): before saying the store has no product for a "
    "place, brand, or kind, search that exact name ('Thailand', 'Bangkok'). "
    "Search matches product names, and an interest is rarely in one: for someone who loves "
    "music, also search the kinds of product that serve it ('speaker', 'record', 'concert'); "
    "for reading, 'book', 'lamp', 'bookmark'. Search those in the same round as the interest. "
    "The catalog has no sales, popularity, or rating data: never call a product a "
    "bestseller, most popular, top-rated, or a favourite, in your words or a card's reason; "
    "for such a request show a varied selection as your picks and say they are picks. "
    "The checkout card's 'Check out securely' button opens the store's own Shopify checkout, "
    "where the customer enters contact, delivery, and payment details and places the order; "
    "say that, rather than that they confirm in the app."
)


class ShopifyError(RuntimeError):
    """A Shopify call failed; the executor reports the tool as temporarily unavailable."""


class ShopifyNotFound(ShopifyError):
    """Shopify answered that the thing asked for does not exist (e.g. product_not_found)."""


class CartRefused(Unavailable):
    """Shopify's cart dropped a line the catalog lists as available. Its message then says
    "already sold out" (live: every shipped product, when the buyer's country is one the
    store does not ship to), which is not true of the item, so it is reported apart."""


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _query_words(query: str) -> list[set[str]]:
    """A query's meaningful words, each with its catalog synonyms, as match groups."""
    words = [_singular(w) for w in re.findall(r"[a-z][a-z'-]+", query.lower())]
    groups = []
    for w in words:
        if w in _STOPWORDS or w in _GENERIC_WORDS or len(w) < 3:
            continue
        groups.append({w, *_SEARCH_SYNONYMS.get(w, ())})
    return groups


def _synonym_queries(query: str) -> list[str]:
    """Extra one-word searches for a shopper word the titles may not use ('hotel' ->
    'villa', 'stay')."""
    words = [_singular(w) for w in re.findall(r"[a-z][a-z'-]+", query.lower())]
    extra = [syn for w in words for syn in _SEARCH_SYNONYMS.get(w, ()) if syn not in words]
    return list(dict.fromkeys(extra))[:3]


def _keywords(text: str) -> set[str]:
    words = {w.rstrip("s") if len(w) > 4 else w for w in re.findall(r"[a-z][a-z-]+", text.lower())}
    words -= _STOPWORDS
    for group in _SYNONYMS:
        if words & {w.rstrip("s") if len(w) > 4 else w for w in group}:
            words |= {f"#{min(group)}"}  # one shared token per synonym group
    return words


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


# Where a store's description template starts: what follows is the same for every product
# ("Hand-picked for quality...", "Product details ..."), and the sentence before it names
# the brand ("Pick of the shelf with iKnowledge."). A search result keeps what comes first.
_TEMPLATE = re.compile(r"\s*(?:Hand-picked\b|Product details\b)", re.IGNORECASE)
_BRAND_LINE = re.compile(r"\s*\b[A-Z][\w' ]{0,40} with [\w.-]+\.\s*$")


def _summary(text: str | None, max_chars: int = 200) -> str | None:
    """The product's own opening words, without the store's per-product template: half of
    a 25-result search was template text, which pushed the result past the runtime's size
    cap and cut it mid-record (the model then saw truncated JSON)."""
    if not text:
        return None
    head = _TEMPLATE.split(text, maxsplit=1)[0]
    head = _BRAND_LINE.sub("", head).strip() or text
    return head[:max_chars].rstrip() or None


def _text(description: Any) -> str | None:
    if isinstance(description, dict):
        description = description.get("plain") or description.get("html")
    if not description:
        return None
    text = " ".join(html.unescape(_TAG.sub(" ", str(description))).split())
    return _RUN_ON.sub(r"\1 ", text) or None


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
        self.domain = domain
        self.ucp_endpoint = f"https://{domain}/api/ucp/mcp"
        self.mcp_endpoint = f"https://{domain}/api/mcp"
        self.profile = agent_profile_url
        self._client_id, self._client_secret = client_id, client_secret
        self._country = buyer_country
        self._currency = "USD"
        self._store_loaded = False
        self._store_lock = asyncio.Lock()
        self._store_retry_at = 0.0
        self._faqs: dict[str, Policy] = {}
        self._index: list[Product] = []
        self._index_until = 0.0
        self._index_lock = asyncio.Lock()
        self._collections: dict[str, list[str]] = {}  # product id -> its collection titles
        self._tags: dict[str, list[str]] = {}
        # Store health, for /healthz and for chips during an outage.
        self._failures: deque[float] = deque(maxlen=50)
        self._cooldown_until = 0.0
        self._last_ok = 0.0
        self.last_error = ""
        self._faqs_loaded_at = 0.0
        self._utm_source = utm_source
        self._http = http or httpx.AsyncClient(timeout=20)
        self._ids = itertools.count(1)
        self._read_cache: dict[str, tuple[float, Any]] = {}
        self._written: dict[str, tuple[float, dict[str, Any]]] = {}
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
        key = None
        if tool in _CACHED_TOOLS:
            key = json.dumps([tool, ucp, arguments], sort_keys=True, default=str)
            hit = self._read_cache.get(key)
            if hit and hit[0] > time.monotonic():
                return copy.deepcopy(hit[1])
        content = await self._call_uncached(tool, arguments, ucp=ucp, auth=auth, headers=headers)
        if key is not None:
            if len(self._read_cache) >= _CACHE_SIZE:
                self._read_cache.clear()
            self._read_cache[key] = (time.monotonic() + _CACHE_SECONDS, copy.deepcopy(content))
        return content

    async def _call_uncached(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        ucp: bool = True,
        auth: bool = False,
        headers: dict[str, str] | None = None,
    ) -> Any:
        read = tool in _READ_TOOLS
        if read and (left := self._cooldown_until - time.monotonic()) > 0:
            raise ShopifyError(f"the store is rate-limiting this server; catalog calls resume in {left:.0f}s")
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
        url = self.ucp_endpoint if ucp else self.mcp_endpoint
        response = await self._post(url, body, headers, _READ_RETRIES if read else _RETRIES)
        if response.status_code in (429, 430):
            try:
                pause = float(response.headers.get("retry-after", ""))
            except ValueError:
                pause = _COOLDOWN_SECONDS
            pause = min(max(pause, _COOLDOWN_SECONDS), _MAX_COOLDOWN_SECONDS)
            self._cooldown_until = time.monotonic() + pause
            logger.warning(
                "shopify throttled %s (HTTP %s): catalog calls pause %.0fs", tool, response.status_code, pause
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
            codes = [str(m.get("code", "")) for m in messages or [] if isinstance(m, dict)]
            error = ShopifyNotFound if any(c.endswith("not_found") for c in codes) else ShopifyError
            # Readable text for the shopper-facing error: Shopify's own wording, else codes.
            said = [str(m.get("content") or m.get("code")) for m in messages or [] if isinstance(m, dict)]
            raise error(f"{tool}: {'; '.join(said) if said else content}")
        return content

    async def _post(
        self, url: str, body: dict[str, Any], headers: dict[str, str], retries: int = _RETRIES
    ) -> httpx.Response:
        """POST, retried when Shopify is rate-limiting or briefly down (429, 502-504).
        Seen live: four chats at once got 429 on cart writes, which reached the shopper as
        "the store did not accept that". Waits Retry-After when given, else 0.5s, 1s, 2s."""
        for attempt in range(retries + 1):
            response = await self._http.post(url, json=body, headers=headers)
            if response.status_code not in _RETRY_STATUSES or attempt == retries:
                if response.status_code in _RETRY_STATUSES:
                    logger.warning("shopify gave up after %d retries: HTTP %s", retries, response.status_code)
                return response
            logger.warning("shopify HTTP %s, retry %d", response.status_code, attempt + 1)
            try:
                wait = float(response.headers.get("retry-after", ""))
            except ValueError:
                wait = 0.5 * 2**attempt
            await asyncio.sleep(min(wait, 8.0))
        return response

    def _context(self) -> dict[str, Any]:
        return {"address_country": self._country} if self._country else {}

    async def _load_store(self) -> None:
        """The store's own country, currency, and shipping countries, from /meta.json, once.
        A buyer country left unset, or set to one the store does not ship to, becomes the
        store's country: otherwise Shopify places the buyer by this server's IP and its cart
        refuses every shipped product as "already sold out" (seen live on Render)."""
        if self._store_loaded or time.monotonic() < self._store_retry_at:
            return
        async with self._store_lock:  # parallel first calls wait for the one fetch
            if self._store_loaded or time.monotonic() < self._store_retry_at:
                return
            try:
                response = await self._http.get(f"https://{self.domain}/meta.json")
                response.raise_for_status()
                meta = response.json()
            except (httpx.HTTPError, ValueError):
                # Keep the configured country for now (non-shipped items still add); retry
                # in a few minutes rather than on every call.
                self._store_retry_at = time.monotonic() + 30
                logger.warning("meta.json unavailable; buyer country stays %s for now", self._country)
                return
            self._store_loaded = True
        self._currency = meta.get("currency") or self._currency
        ships_to = [str(c).upper() for c in meta.get("ships_to_countries") or []]
        configured = (self._country or "").upper()
        if not configured or (ships_to and configured not in ships_to):
            fallback = meta.get("country") if meta.get("country") in ships_to or not ships_to else None
            self._country = fallback or (ships_to[0] if ships_to else self._country)

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
        # Most specific first: "Books" before "Retail and Gifts".
        collections = sorted(
            (c["title"] for c in raw.get("collections") or [] if isinstance(c, dict) and c.get("title")),
            key=lambda title: " and " in title.lower(),
        )
        product = Product(
            product_id=raw["id"],
            title=raw.get("title", raw["id"]),
            brand=raw.get("vendor") or ((variants[0].get("seller") or {}).get("name") if variants else None),
            price=_money(low),
            currency=low.get("currency", "USD") if isinstance(low, dict) else "USD",
            image_url=media[0].get("url") if media and isinstance(media[0], dict) else None,
            in_stock=bool(available) if variants else True,
            short_description=_summary(_text(raw.get("description"))),
            options=options,
            category=collections[0] if collections else None,
            attributes={"product_url": self._product_url(raw), **self._facts(raw, low)},
        )
        self._collections[product.product_id] = collections
        self._tags[product.product_id] = [str(t) for t in raw.get("tags") or []]
        if not options and len(variants) == 1:
            self._default_variant[product.product_id] = variants[0]["id"]
        return product

    @staticmethod
    def _facts(raw: dict[str, Any], low: Any) -> dict[str, str]:
        """Comparable facts from the title and description (facts.py), for comparisons."""
        variants = raw.get("variants") or []
        ships = (variants[0].get("requires") or {}).get("shipping") if variants else None
        currency = low.get("currency", "") if isinstance(low, dict) else ""
        price = _money(low) if low else None
        return extract(raw.get("title", ""), _text(raw.get("description")), price, currency, ships)

    def _product_url(self, raw: dict[str, Any]) -> str:
        """Where a shopper opens the product on the storefront: the catalog's own link when
        it gives one, the /products/{handle} page, else a storefront search for the title."""
        url = raw.get("url") or raw.get("online_store_url") or raw.get("onlineStoreUrl")
        if not url and raw.get("handle"):
            url = f"https://{self.domain}/products/{raw['handle']}"
        if not url:
            url = f"https://{self.domain}/search?" + urlencode(
                {"q": raw.get("title") or "", "type": "product"}
            )
        return self._attributed(url)

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
        await self._load_store()
        ranked = await self._ranked(query, filters, limit)
        if ranked is not None:
            return ranked
        try:
            products = await self._search(query, filters, limit)
            extra = [(q, "place") for q in expansions(query)] + [(q, "word") for q in _synonym_queries(query)]
            if extra:
                # Travel products are titled by city ("Kuala Lumpur City Tour"), not country;
                # a shopper's word may not be the title's ("hotel" -> "Bali Villa Stay").
                found = await asyncio.gather(*(self._search(q, filters, limit) for q, _ in extra))
                seen = {p.product_id for p in products}
                for (q, kind), batch in zip(extra, found, strict=True):
                    place = next((x for x in PLACE_NAMES if x in q), None) if kind == "place" else None
                    for p in batch:
                        text = f"{p.title} {p.short_description or ''}".lower()
                        named = (place in text) if place else bool(re.search(rf"\b{re.escape(q)}", text))
                        if named and p.product_id not in seen:  # Shopify's fuzzy matches dropped
                            seen.add(p.product_id)
                            products.append(p)
                products = products[: max(limit, 12)]
            if not products and _RANKING_WORDS.search(query):
                # Live: "bestseller" (sorted by rating) found nothing; the catalog has no sales
                # or rating data, so drop the ranking words and fall back to relevance.
                rest = " ".join(_RANKING_WORDS.sub(" ", query).split())
                products = await self._search(rest, filters, limit)
        except (ShopifyError, httpx.HTTPError) as error:
            if isinstance(error, ShopifyNotFound):
                raise
            # Shopify's search is failing (live: rate limited during a test crawl). Answer
            # from the catalog index read earlier rather than with an error.
            self._failed(error)
            fallback = self._index_match(query)
            if not fallback:
                raise ShopifyError(
                    "the store's search is not answering right now (busy or rate-limited); "
                    "try again in a minute"
                ) from error
            logger.warning("search %r answered from the catalog index (%s)", query, error)
            return self._filtered(fallback, filters)[:limit]
        self._ok()
        return products

    # -- the catalog index ---------------------------------------------------------------

    async def catalog_index(self) -> list[Product]:
        """Every sale-ready product, read page by page and kept for an hour. A failed read
        keeps the last good index and tries again in a minute."""
        if self._index and self._index_until > time.monotonic():
            return self._index
        async with self._index_lock:
            if self._index and self._index_until > time.monotonic():
                return self._index
            await self._load_store()
            products: dict[str, Product] = {}
            cursor = None
            try:
                for _ in range(_INDEX_MAX_PAGES):
                    page: dict[str, Any] = {"limit": _INDEX_PAGE} | ({"cursor": cursor} if cursor else {})
                    catalog: dict[str, Any] = {"query": "", "pagination": page}
                    if self._context():
                        catalog["context"] = self._context()
                    content = await self._call_uncached("search_catalog", {"catalog": catalog})
                    for raw in content.get("products") or []:
                        product = self._product(raw)
                        products.setdefault(product.product_id, product)
                    more = content.get("pagination") or {}
                    cursor = more.get("cursor")
                    if not more.get("has_next_page") or not cursor:
                        break
            except (ShopifyError, httpx.HTTPError, ValueError, KeyError) as error:
                self._failed(error)
                if not self._index:
                    self._load_snapshot()
                logger.warning("catalog index read failed (%s); keeping %d products", error, len(self._index))
                self._index_until = time.monotonic() + 60
                return self._index
            self._ok()
            self._index = list(products.values())
            self._index_until = time.monotonic() + _INDEX_SECONDS
            logger.info("catalog index: %d products", len(self._index))
            return self._index

    def snapshot(self) -> dict[str, Any]:
        """The index as JSON, for SNAPSHOT_PATH."""
        return {
            "store": self.domain,
            "products": [
                {
                    "product": p.model_dump(exclude_none=True),
                    "collections": self._collections.get(p.product_id, []),
                    "tags": self._tags.get(p.product_id, []),
                    "variant_id": self._default_variant.get(p.product_id),
                }
                for p in self._index
            ],
        }

    def _load_snapshot(self, path: Path | None = None) -> None:
        path = path or SNAPSHOT_PATH
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        if data.get("store") != self.domain:
            return  # another store's catalog
        for row in data.get("products") or []:
            product = Product.model_validate(row["product"])
            self._index.append(product)
            self._collections[product.product_id] = row.get("collections") or []
            self._tags[product.product_id] = row.get("tags") or []
            if row.get("variant_id"):
                self._default_variant.setdefault(product.product_id, row["variant_id"])
        logger.warning("catalog index from the shipped snapshot: %d products", len(self._index))

    def collections(self) -> dict[str, list[Product]]:
        """The index by collection, most specific collection per product, largest first."""
        groups: dict[str, list[Product]] = {}
        for product in self._index:
            groups.setdefault(product.category or "Other", []).append(product)
        return dict(sorted(groups.items(), key=lambda kv: -len(kv[1])))

    def vocabulary(self) -> set[str]:
        """Words that name something the store sells: titles, collections, tags."""
        words: set[str] = set()
        for p in self._index:
            text = " ".join(
                [p.title, *self._collections.get(p.product_id, []), *self._tags.get(p.product_id, [])]
            )
            words |= {_singular(w) for w in re.findall(r"[a-z][a-z'-]+", text.lower()) if len(w) > 2}
        return words - _STOPWORDS

    def _index_match(self, query: str) -> list[Product]:
        """Index products naming the query's words (synonyms included), best match first;
        the whole index when the query names nothing in particular."""
        words = _query_words(query)
        if not words:
            return list(self._index)
        scored = []
        for p in self._index:
            # A word in the title counts most, then the summary, then collection and tags.
            fields = (
                (3, p.title.lower()),
                (2, (p.short_description or "").lower()),
                (
                    1,
                    " ".join(
                        [*self._collections.get(p.product_id, []), *self._tags.get(p.product_id, [])]
                    ).lower(),
                ),
            )
            score = sum(
                max(
                    (w for w, text in fields if any(re.search(rf"\b{re.escape(x)}", text) for x in group)),
                    default=0,
                )
                for group in words
            )
            if score:
                scored.append((score, p))
        scored.sort(key=lambda sp: -sp[0])
        return [p for _, p in scored]

    @staticmethod
    def _filtered(products: list[Product], filters: SearchFilters) -> list[Product]:
        return [
            p
            for p in products
            if (filters.min_price is None or p.price >= filters.min_price)
            and (filters.max_price is None or p.price <= filters.max_price)
        ]

    async def _ranked(self, query: str, filters: SearchFilters, limit: int) -> list[Product] | None:
        """A price ranking over the whole catalog ('most expensive', 'cheapest tour', or a
        price sort), or None for an ordinary search."""
        word = _SUPERLATIVE.search(query)
        if not word and filters.sort not in ("price_asc", "price_desc"):
            return None
        index = await self.catalog_index()
        if not index:
            return None
        rest = _SUPERLATIVE.sub(" ", query)
        matches = self._filtered(self._index_match(rest), filters)
        if not matches:
            return None
        ascending = bool(word and _ASCENDING.search(word.group(0))) if word else filters.sort == "price_asc"
        matches.sort(key=lambda p: p.price, reverse=not ascending)
        return matches[:limit]

    # -- store health --------------------------------------------------------------------

    def _failed(self, error: Exception) -> None:
        self._failures.append(time.monotonic())
        self.last_error = f"{type(error).__name__}: {error}"[:200]

    def _ok(self) -> None:
        self._last_ok = time.monotonic()

    def degraded(self) -> bool:
        """Several catalog calls failed lately and none has worked since, or Shopify is
        throttling this server."""
        now = time.monotonic()
        if self._cooldown_until > now:
            return True
        recent = [t for t in self._failures if now - t < _DEGRADED_WINDOW and t > self._last_ok]
        return len(recent) >= _DEGRADED_FAILURES

    def health(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "degraded": self.degraded(),
            "catalog_paused_seconds": max(0, round(self._cooldown_until - now)),
            "failures_last_5m": sum(1 for t in self._failures if now - t < 300),
            "last_error": self.last_error or None,
            "catalog_products": len(self._index),
            "buyer_country": self._country,
            "store_meta_loaded": self._store_loaded,
        }

    async def _search(self, query: str, filters: SearchFilters, limit: int) -> list[Product]:
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
        await self._load_store()
        catalog: dict[str, Any] = {"id": product_id}
        if selected:
            catalog["selected"] = selected
        if self._context():
            catalog["context"] = self._context()
        try:
            content = await self._call("get_product", {"catalog": catalog})
        except ShopifyNotFound:
            # Live: an unknown id is an isError "product_not_found". Answering None lets the
            # agent say there is no such product instead of "the lookup isn't working".
            return None
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
        # Every photo, for the in-chat product page (search keeps just the first).
        photos = [m["url"] for m in raw.get("media") or [] if isinstance(m, dict) and m.get("url")]
        if len(photos) > 1:
            details["attributes"] = {**family.attributes, "image_urls": " ".join(photos[:8])}
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
        # The cart this backend wrote moments ago, rather than reading it back before every
        # change and every cap check: most cart calls, which a busy store rate-limits first
        # (live: 429 on cart writes). Older than _FRESH_CART_SECONDS: read it from Shopify.
        fresh = self._written.get(session.session_id)
        if fresh and fresh[0] > time.monotonic():
            return copy.deepcopy(fresh[1])
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
        """Write the whole cart (update_cart replaces everything it is sent). A refusal while
        the store's country is still unknown (meta.json failed, e.g. rate limited) reads it
        now and writes once more: a wrong buyer country refuses every shipped item."""
        try:
            return await self._write_lines(session, lines, changed, variant_id)
        except CartRefused:
            if self._store_loaded:
                raise
            before = self._country
            self._store_retry_at = 0.0
            await self._load_store()
            if self._country == before:
                raise
            logger.warning("cart refused with buyer country %s; retrying with %s", before, self._country)
            return await self._write_lines(session, lines, changed, variant_id)

    async def _write_lines(
        self,
        session: ShoppingSessionContext,
        lines: dict[str, int],
        changed: str,
        variant_id: str | None = None,
    ) -> Cart:
        await self._load_store()
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
            self._written.pop(session.session_id, None)
            return Cart(currency=self._currency)
        if cart_id:
            content = await self._call("update_cart", {"id": cart_id, "cart": payload})
        else:
            content = await self._call("create_cart", {"cart": payload})
        raw = content.get("cart") or content
        self._cart_ids[session.session_id] = raw.get("id", cart_id)
        self._written[session.session_id] = (time.monotonic() + _FRESH_CART_SECONDS, copy.deepcopy(raw))
        self._raise_if_unavailable(raw, changed)
        # Shopify can also drop a line with only a warning (e.g. merchandise_out_of_stock when
        # inventory is 0), so check that the line written is actually in the returned cart.
        variant_id = variant_id or changed
        if lines.get(variant_id) and variant_id not in {
            (line.get("item") or {}).get("id") for line in raw.get("line_items") or []
        }:
            reasons = "; ".join(m.get("content") or m.get("code", "") for m in raw.get("messages") or [])
            detail = f" ({reasons})" if reasons else ""
            known = self._variants.get(variant_id)
            if known is not None and not known.in_stock:
                raise Unavailable(f"{changed} could not be added to the cart{detail}")
            # The catalog says available: the cart's refusal is not a stock-out.
            raise CartRefused(f"{changed} could not be added to the cart{detail}")
        return self._cart(session, raw)

    async def _lines(self, session: ShoppingSessionContext) -> dict[str, int]:
        raw = await self._raw_cart(session)
        return {
            (line.get("item") or {}).get("id"): line.get("quantity", 1)
            for line in (raw or {}).get("line_items") or []
        }

    async def get_cart(self, session: ShoppingSessionContext) -> Cart:
        raw = await self._raw_cart(session)
        await self._load_store()
        return self._cart(session, raw) if raw else Cart(currency=self._currency)

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

    def _line_for(self, lines: dict[str, int], product_id: str) -> str:
        """The cart line (a variant id) a product id refers to. Cart lines are variants, but a
        single-variant product is added by its product id; seen live, "remove the eSIM" then
        matched no line, the cart came back unchanged, and the agent said it was removed."""
        if product_id in lines:
            return product_id
        matches = [
            vid
            for vid in lines
            if vid == self._default_variant.get(product_id)
            or getattr(self._variants.get(vid), "variant_of", None) == product_id
        ]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise Unavailable(
                f"{product_id} has {len(matches)} lines in the cart ({', '.join(matches)}); "
                "name the one to change"
            )
        raise Unavailable(f"{product_id} is not in the cart; nothing was changed")

    async def update_cart_item(self, session: ShoppingSessionContext, product_id: str, quantity: int) -> Cart:
        lines = await self._lines(session)
        line = self._line_for(lines, product_id)
        lines[line] = quantity
        return await self._put_lines(session, lines, product_id, line)

    async def remove_from_cart(self, session: ShoppingSessionContext, product_id: str) -> Cart:
        lines = await self._lines(session)
        del lines[self._line_for(lines, product_id)]
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
            # The cart the executor just read, rather than a second read that a busy store
            # can refuse (live: a 429 there failed checkout).
            lines = {i.product_id: i.quantity for i in cart.items} or await self._lines(session)
            checkout: dict[str, Any] = {
                "line_items": [{"quantity": q, "item": {"id": vid}} for vid, q in lines.items()]
            }
            if self._context():
                checkout["context"] = self._context()
            checkout_args = {
                "checkout": checkout,
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
        """Shopify's answers for the query, then the store's FAQ entries that share its
        keywords (synonyms included), best match first. Shopify alone misses an entry asked
        in other words; when nothing matches either way, the general return and shipping
        policies, which settle many questions."""
        found = {p.policy_id: p for p in await self._policies(query)}
        faqs = await self._faq_list()
        for p in found.values():
            faqs.setdefault(p.policy_id, p)
        wanted = _keywords(query)
        # A keyword in the question counts double one in the answer.
        scored = sorted(
            (
                (2 * len(wanted & _keywords(p.title)) + len(wanted & _keywords(p.content)), p)
                for p in faqs.values()
            ),
            key=lambda pair: -pair[0],
        )
        for score, policy in scored:
            if score and len(found) < 5:
                found.setdefault(policy.policy_id, policy)
        if found:
            return list(found.values())
        return [p for p in faqs.values() if p.title in _FALLBACK_POLICY_QUERIES] or [
            p for q in _FALLBACK_POLICY_QUERIES for p in await self._policies(q)
        ]

    async def _faq_list(self) -> dict[str, Policy]:
        """The store's FAQ entries, gathered from the seed questions; refreshed hourly."""
        if not self._faqs or time.monotonic() - self._faqs_loaded_at > _FAQ_TTL_SECONDS:
            batches = await asyncio.gather(
                *(self._policies(q) for q in _FAQ_SEED_QUERIES), return_exceptions=True
            )
            faqs = {p.policy_id: p for batch in batches if isinstance(batch, list) for p in batch}
            if faqs:
                self._faqs, self._faqs_loaded_at = faqs, time.monotonic()
        return self._faqs

    async def _policies(self, query: str) -> list[Policy]:
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
    merged = defaults | overrides
    merged["domain_search_notes"] = f"{merged['domain_search_notes']} {SHOPIFY_PROMPT_NOTES}".strip()
    return ShoppingAgentConfig(**merged)
