"""A minimal host for the shopping agent: sessions, streamed chat, cart, checkout webhook.

    export ANTHROPIC_API_KEY=...   # or STORE_AGENT_ANTHROPIC_API_KEY (see below)
    uvicorn my_store.app:app --port 8000

    curl -s -XPOST localhost:8000/api/session -H 'content-type: application/json' -d '{}'
    curl -N -XPOST localhost:8000/api/chat -H "X-Session-Id: <id>" \
         -H 'content-type: application/json' -d '{"message": "a 2-person tent under $250"}'

This is the shape of ``vendor/commerce-agents/examples/demo_common/storefront.py`` cut down
to the essentials. ``guards.py`` adds rate limits and a daily ceiling for a public launch
(DEPLOY.md). Still owed before scale: a durable session store (Redis/DB) so chats survive a
restart, and signature checks on the payment webhook
(``vendor/commerce-agents/docs/safety.md``, "What a deployment owns").
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from commerce_common.memory import InMemoryMemoryStore
from commerce_common.streaming import AgentEvent, to_sse
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from shopping_agent import (
    PageContext,
    ShoppingAgentConfig,
    ShoppingSessionContext,
    ShoppingSessionState,
)
from shopping_agent.serialization import cart_payload
from shopping_agent_runtime import ShoppingAgent

from . import discovery, guards
from .backend import MyStoreBackend
from .executor import StoreToolExecutor
from .industry import industry_config_overrides, industry_extensions
from .shopify_backend import ShopifyUCPBackend, shopify_agent_config

logger = logging.getLogger(__name__)

# Claude Code cloud environments reserve ANTHROPIC_API_KEY for their own sign-in and drop a
# value set there, so the agent's key can come in under this name instead.
if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("STORE_AGENT_ANTHROPIC_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = os.environ["STORE_AGENT_ANTHROPIC_API_KEY"]

# The reference skills with a products-first rule: no intake questions or product-less outlines
# before the first recommendation (my_store/skills/README.md lists the changes).
SKILLS_DIR = Path(__file__).resolve().parent / "skills"

# STORE_BACKEND=shopify (with SHOPIFY_STORE_DOMAIN set) runs on a real Shopify store;
# otherwise the sample catalog.json store.
if os.environ.get("STORE_BACKEND") == "shopify":
    backend: MyStoreBackend | ShopifyUCPBackend = ShopifyUCPBackend.from_env()
    config = shopify_agent_config(**industry_config_overrides())
else:
    backend = MyStoreBackend()
    config = ShoppingAgentConfig(
        brand_name="Trailhead Supply",
        assistant_name="Trail Guide",
        brand_voice="friendly, outdoorsy, and brief",
        # Switch off systems you don't have, e.g. enable_orders=False on a referral surface.
    )
# A shopping request's first round is a product search, before any question (discovery.py).
discovery.install()
agent = ShoppingAgent(
    backend=backend,
    skills_dir=SKILLS_DIR,
    config=config,
    memory_store=InMemoryMemoryStore(),  # swap for a durable MemoryStore
    # Travel itineraries and plan tables from the reference verticals (industry.py).
    extra_presentation_tools=industry_extensions(),
    # Cart rules from the live UAT: no add before a same-round search returns, the store's
    # own error text, cart lines count as seen (executor.py).
    executor_class=StoreToolExecutor,
)


@dataclass
class Session:
    session_id: str
    user_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    state: ShoppingSessionState = field(default_factory=ShoppingSessionState)
    pending_app_events: list[str] = field(default_factory=list)
    turns: int = 0
    last_seen: float = field(default_factory=time.monotonic)


SESSIONS: dict[str, Session] = {}  # TODO(live): Redis or your app's session store
app = FastAPI(title="Shopping agent")
STATIC = Path(__file__).parent / "static"


class StartSession(BaseModel):
    """No fields the host trusts. Every chat used to default to user_id "demo-user", so all
    anonymous shoppers shared one long-term memory: in the live UAT a fresh chat answered
    "you're planning a trip to Japan" from another shopper's session. A client-sent id is
    ignored too, since without sign-in anyone could claim someone else's.
    TODO(live): take the user id from your auth (e.g. a signed-in Shopify customer)."""


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    page: PageContext | None = None
    # "chip" when the shopper tapped a suggestion: that turn always searches the catalog.
    source: str | None = Field(default=None, max_length=16)


def current(session_id: str | None) -> Session:
    if not session_id or session_id not in SESSIONS:
        raise HTTPException(401, "Unknown session")
    s = SESSIONS[session_id]
    s.last_seen = time.monotonic()
    return s


def drop_idle_sessions() -> None:
    cutoff = time.monotonic() - guards.SESSION_IDLE_SECONDS
    for sid in [sid for sid, s in SESSIONS.items() if s.last_seen < cutoff]:
        del SESSIONS[sid]


def _text(block: Any) -> str:
    kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
    text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
    return text if kind == "text" and isinstance(text, str) else ""


def context(s: Session, page: PageContext | None = None) -> ShoppingSessionContext:
    return ShoppingSessionContext(
        session_id=s.session_id, user_id=s.user_id, page=page or PageContext(), now=datetime.now()
    )


@app.get("/", include_in_schema=False)
async def chat_page() -> FileResponse:
    """The browser chat over the API below; the storefront widget frames it (?embed=1)."""
    return FileResponse(STATIC / "chat.html")


@app.get("/widget.js", include_in_schema=False)
async def widget() -> FileResponse:
    """The chat bubble a Shopify theme loads with one script tag (DEPLOY.md)."""
    return FileResponse(STATIC / "widget.js", media_type="text/javascript")


# The Shopify stores whose catalog this deployment serves. widget.js shows the bubble only
# on these, so the same script tag on another store (a trial store) shows no chat that
# would sell it this store's products. Comma-separated myshopify domains.
WIDGET_SHOPS = [
    d.strip().lower()
    for d in os.environ.get("WIDGET_SHOPS", os.environ.get("SHOPIFY_STORE_DOMAIN", "")).split(",")
    if d.strip()
]


@app.get("/api/widget-config", include_in_schema=False)
async def widget_config() -> JSONResponse:
    # Read cross-origin by widget.js on the store's own domain; the list is public.
    return JSONResponse({"shops": WIDGET_SHOPS}, headers={"Access-Control-Allow-Origin": "*"})


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    return {"ok": True}


@app.post("/api/session")
async def start_session(body: StartSession, request: Request) -> dict:
    guards.session_limiter.check(guards.client_ip(request), "Too many new chats. Please wait a bit.")
    drop_idle_sessions()
    s = Session(session_id=secrets.token_urlsafe(24), user_id=f"guest-{secrets.token_urlsafe(12)}")
    SESSIONS[s.session_id] = s
    return {"session_id": s.session_id}


@app.post("/api/chat")
async def chat(body: ChatRequest, request: Request, x_session_id: str | None = Header(default=None)):
    s = current(x_session_id)
    ip = guards.client_ip(request)
    guards.chat_limiter.check(ip, "You're sending messages quickly. Please wait a moment.")
    guards.token_budget.check()
    if s.turns >= guards.TURNS_PER_SESSION:
        raise HTTPException(429, "This chat has reached its length limit. Start a new chat to continue.")
    guards.ip_daily.check(ip, "You've reached today's chat limit. Please come back tomorrow.")
    guards.daily_budget.spend()
    s.turns += 1
    if isinstance(backend, ShopifyUCPBackend):
        backend.set_buyer_ip(s.session_id, ip)
    if s.pending_app_events:  # things that happened outside the chat (e.g. payment)
        note = "[App events since your last reply: " + " ".join(s.pending_app_events) + "]"
        s.pending_app_events.clear()
        content = [{"type": "text", "text": note}, {"type": "text", "text": body.message}]
        s.messages.append({"role": "user", "content": content})
    else:
        s.messages.append({"role": "user", "content": body.message})
    ctx = context(s, body.page)
    chip = body.source == "chip"
    if chip:
        discovery.chip_tapped(s.state)
    # A cart chip ("Add ...", "Check out") needs no product cards after it.
    search_chip = chip and not discovery.is_cart_or_signoff(body.message)

    async def turn():
        # Every reply ends with chips: when the model gave none, the host adds them from the
        # products this reply showed, so the shopper always has a next step to tap.
        # A tapped chip always leads to products: when its reply showed none, the host adds
        # the closest ones found, above the chips (which it holds back until then).
        has_chips, titles, held = False, [], []
        try:
            # Events: text_delta, tool_call, ui (render the component), cart_update, turn_complete
            async for event in agent.stream_turn(s.messages, ctx, s.state):
                if event.type == "turn_complete":
                    guards.token_budget.charge(event.data.get("usage") or {})
                if event.type == "ui":
                    component, payload = event.data.get("component"), event.data.get("payload") or {}
                    has_chips |= component == "suggestions"
                    titles += discovery.product_titles(component, payload)
                    if search_chip and component == "suggestions":
                        held.append(event)
                        continue
                yield to_sse(event)
            failed = False
        except Exception:
            logger.exception("chat turn failed")
            yield to_sse(AgentEvent.error("Something went wrong. Please try again."))
            failed = True
        if search_chip and not titles and not failed:
            cards = discovery.fallback_products(s.state)
            if cards:
                titles += [item["product"]["title"] for item in cards["items"]]
                yield to_sse(AgentEvent.ui("products", cards))
        for event in held:
            yield to_sse(event)
        if not has_chips:
            chips = discovery.fallback_chips(titles)
            yield to_sse(AgentEvent.ui("suggestions", {"suggestions": chips}))
        if not failed:
            await agent.update_memory(s.messages, ctx)

    return StreamingResponse(
        with_keepalive(turn()),
        media_type="text/event-stream",
        # No proxy buffering or caching: each event reaches the phone as it happens.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


KEEPALIVE_SECONDS = 10


async def with_keepalive(events: AsyncIterator[str]) -> AsyncIterator[str]:
    """Pass the turn's events through, plus an SSE comment line whenever none has gone out
    for KEEPALIVE_SECONDS. A turn that spends a while in tool calls (a comparison looking up
    several products) otherwise sends nothing, and a proxy or a phone's browser can drop the
    idle stream mid-turn."""
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for item in events:
                await queue.put(item)
        finally:
            await queue.put(None)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if item is None:
                break
            yield item
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@app.get("/api/history")
async def history(x_session_id: str | None = Header(default=None)) -> dict:
    """The chat's text turns, so the widget can redraw after the shopper changes page."""
    s = current(x_session_id)
    turns = []
    for m in s.messages:
        content = m.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        text = "\n\n".join(t for t in map(_text, blocks) if t and not t.startswith("[App events"))
        if text and m.get("role") in ("user", "assistant"):
            turns.append({"role": m["role"], "text": text})
    return {"turns": turns}


@app.get("/api/cart")
async def get_cart(x_session_id: str | None = Header(default=None)) -> dict:
    s = current(x_session_id)
    return cart_payload(await backend.get_cart(context(s)))


# -- Happy-path buttons --------------------------------------------------------------------
# A recommendation card's buttons act directly, without a model turn: choose a variant, add,
# check out. Each runs through the agent's own executor, so the provenance gate (only
# products this chat has shown), option and stock checks, and quantity caps still hold, and
# the agent is told on its next turn what the shopper did.


class CartAdd(BaseModel):
    product_id: str = Field(min_length=1, max_length=200)
    quantity: int = Field(default=1, ge=1, le=99)


class ProductRef(BaseModel):
    product_id: str = Field(min_length=1, max_length=200)


def executor_for(s: Session):
    return agent.executor_class(
        backend=backend,
        config=agent.config,
        skills=agent.skills,
        session=context(s),
        state=s.state,
        memory=agent.memory,
        extensions=agent.extra_presentation_tools,
    )


def _first_sentence(text: str) -> str:
    text = " ".join(text.split())
    return (text.split(". ")[0].rstrip(".") + ".")[:240] if text else "That didn't work."


@app.post("/api/product")
async def product_page(body: ProductRef, x_session_id: str | None = Header(default=None)) -> dict:
    """The in-chat product page for a product this chat showed: photos, description, specs,
    and variants for the option buttons. Opening it counts as a lookup, so the variants it
    lists can go straight into the cart."""
    s = current(x_session_id)
    if body.product_id not in s.state.seen_products:
        raise HTTPException(400, "Ask the assistant about this product first.")
    details = await backend.get_product_details(context(s), body.product_id)
    if details is None:
        raise HTTPException(404, "This product is no longer available.")
    s.state.remember_products([details, *details.variants])  # as the agent's own lookup does
    attributes = dict(details.attributes)
    images = [u for u in attributes.pop("image_urls", "").split() if u] or (
        [details.image_url] if details.image_url else []
    )
    return {
        "product_id": details.product_id,
        "title": details.title,
        "brand": details.brand,
        "price": details.price,
        "currency": details.currency,
        "in_stock": details.in_stock,
        "images": images,
        "description": details.long_description or details.short_description,
        "specs": details.specs,
        "highlights": details.review_highlights,
        "product_url": attributes.get("product_url"),
        "options": details.options,
        "variants": [
            {
                "product_id": v.product_id,
                "option_values": v.option_values,
                "price": v.price,
                "currency": v.currency,
                "in_stock": v.in_stock,
            }
            for v in details.variants
        ],
    }


# The card's option buttons read the same record.
app.post("/api/product/options")(product_page)


@app.post("/api/cart/add")
async def cart_add(body: CartAdd, x_session_id: str | None = Header(default=None)) -> dict:
    s = current(x_session_id)
    out = await executor_for(s).execute("add_to_cart", body.model_dump())
    if out.blocked or out.is_error:
        raise HTTPException(400, _first_sentence(out.result_text))
    product = s.state.seen_products.get(body.product_id)
    title = (product.title if product else body.product_id)[:120]
    s.pending_app_events.append(
        f"Customer tapped Add to cart on {title} ({body.product_id}), quantity {body.quantity}."
    )
    return cart_payload(await backend.get_cart(context(s)))


@app.post("/api/checkout")
async def checkout(x_session_id: str | None = Header(default=None)) -> dict:
    """Stage the cart and return the checkout card (with the Shopify checkout link)."""
    s = current(x_session_id)
    out = await executor_for(s).execute("checkout", {})
    card = next((e.data.get("payload") for e in out.events if e.type == "ui"), None)
    if out.is_error or out.blocked or card is None:
        raise HTTPException(400, _first_sentence(out.result_text))
    s.pending_app_events.append("Customer tapped Checkout; the checkout link was shown.")
    return card


@app.post("/webhooks/checkout-complete/{token}")
async def checkout_complete(token: str) -> dict:
    """Your payment provider calls this when the hosted checkout is paid.
    TODO(live): verify the provider's signature before trusting it. On Shopify, subscribe
    to Shopify's order webhooks instead and queue the same app event."""
    if not isinstance(backend, MyStoreBackend):
        raise HTTPException(404, "Not used with this backend")
    session_id = backend.checkout_tokens.get(token, (None, None))[0]
    order = backend.record_paid_order(token)
    if order is None:
        raise HTTPException(404, "Unknown checkout")
    if session_id in SESSIONS:
        SESSIONS[session_id].pending_app_events.append(
            f"Customer completed payment; order {order.order_id} was placed."
        )
    return {"ok": True, "order_id": order.order_id}
