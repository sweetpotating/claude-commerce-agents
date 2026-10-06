"""A minimal host for the shopping agent: sessions, streamed chat, cart, checkout webhook.

    export ANTHROPIC_API_KEY=...   # or STORE_AGENT_ANTHROPIC_API_KEY (see below)
    uvicorn my_store.app:app --port 8000

    curl -s -XPOST localhost:8000/api/session -H 'content-type: application/json' \
         -d '{"user_id": "demo-user"}'
    curl -N -XPOST localhost:8000/api/chat -H "X-Session-Id: <id>" \
         -H 'content-type: application/json' -d '{"message": "a 2-person tent under $250"}'

This is the shape of ``vendor/commerce-agents/examples/demo_common/storefront.py`` cut down
to the essentials. ``guards.py`` adds rate limits and a daily ceiling for a public launch
(DEPLOY.md). Still owed before scale: a durable session store (Redis/DB) so chats survive a
restart, and signature checks on the payment webhook
(``vendor/commerce-agents/docs/safety.md``, "What a deployment owns").
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from commerce_common.memory import InMemoryMemoryStore
from commerce_common.streaming import AgentEvent, to_sse
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from shopping_agent import (
    PageContext,
    ShoppingAgentConfig,
    ShoppingSessionContext,
    ShoppingSessionState,
)
from shopping_agent.serialization import cart_payload
from shopping_agent_runtime import ShoppingAgent

from . import guards
from .backend import MyStoreBackend
from .industry import industry_config_overrides, industry_extensions
from .shopify_backend import ShopifyUCPBackend, shopify_agent_config

# Claude Code cloud environments reserve ANTHROPIC_API_KEY for their own sign-in and drop a
# value set there, so the agent's key can come in under this name instead.
if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("STORE_AGENT_ANTHROPIC_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = os.environ["STORE_AGENT_ANTHROPIC_API_KEY"]

SKILLS_DIR = Path(__file__).resolve().parents[1] / "vendor/commerce-agents/shopping-agent/skills"

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
agent = ShoppingAgent(
    backend=backend,
    skills_dir=SKILLS_DIR,
    config=config,
    memory_store=InMemoryMemoryStore(),  # swap for a durable MemoryStore
    # Travel itineraries and plan tables from the reference verticals (industry.py).
    extra_presentation_tools=industry_extensions(),
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
    user_id: str = Field(default="demo-user", max_length=64)  # TODO(live): from your auth


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    page: PageContext | None = None


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


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    return {"ok": True}


@app.post("/api/session")
async def start_session(body: StartSession, request: Request) -> dict:
    guards.session_limiter.check(guards.client_ip(request), "Too many new chats. Please wait a bit.")
    drop_idle_sessions()
    s = Session(session_id=secrets.token_urlsafe(24), user_id=body.user_id)
    SESSIONS[s.session_id] = s
    return {"session_id": s.session_id}


@app.post("/api/chat")
async def chat(body: ChatRequest, request: Request, x_session_id: str | None = Header(default=None)):
    s = current(x_session_id)
    ip = guards.client_ip(request)
    guards.chat_limiter.check(ip, "You're sending messages quickly. Please wait a moment.")
    if s.turns >= guards.TURNS_PER_SESSION:
        raise HTTPException(429, "This chat has reached its length limit. Start a new chat to continue.")
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

    async def stream():
        try:
            # Events: text_delta, tool_call, ui (render the component), cart_update, turn_complete
            async for event in agent.stream_turn(s.messages, ctx, s.state):
                yield to_sse(event)
        except Exception:
            yield to_sse(AgentEvent.error("Something went wrong. Please try again."))
            return
        await agent.update_memory(s.messages, ctx)

    return StreamingResponse(stream(), media_type="text/event-stream")


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
