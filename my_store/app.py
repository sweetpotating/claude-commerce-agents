"""A minimal host for the shopping agent: sessions, streamed chat, cart, checkout webhook.

    export ANTHROPIC_API_KEY=...
    uvicorn my_store.app:app --port 8000

    curl -s -XPOST localhost:8000/api/session -H 'content-type: application/json' \
         -d '{"user_id": "demo-user"}'
    curl -N -XPOST localhost:8000/api/chat -H "X-Session-Id: <id>" \
         -H 'content-type: application/json' -d '{"message": "a 2-person tent under $250"}'

This is the shape of ``vendor/commerce-agents/examples/demo_common/storefront.py`` cut down
to the essentials. Before production add: real authentication at session start, a durable
session store (Redis/DB), rate limits, and signature checks on the payment webhook
(``vendor/commerce-agents/docs/safety.md``, "What a deployment owns").
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from commerce_common.memory import InMemoryMemoryStore
from commerce_common.streaming import AgentEvent, to_sse
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from shopping_agent import (
    PageContext,
    ShoppingAgentConfig,
    ShoppingSessionContext,
    ShoppingSessionState,
)
from shopping_agent.serialization import cart_payload
from shopping_agent_runtime import ShoppingAgent

from .backend import MyStoreBackend

SKILLS_DIR = Path(__file__).resolve().parents[1] / "vendor/commerce-agents/shopping-agent/skills"

backend = MyStoreBackend()
agent = ShoppingAgent(
    backend=backend,
    skills_dir=SKILLS_DIR,
    config=ShoppingAgentConfig(
        brand_name="Trailhead Supply",
        assistant_name="Trail Guide",
        brand_voice="friendly, outdoorsy, and brief",
        # Switch off systems you don't have, e.g. enable_orders=False on a referral surface.
    ),
    memory_store=InMemoryMemoryStore(),  # swap for a durable MemoryStore
)


@dataclass
class Session:
    session_id: str
    user_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    state: ShoppingSessionState = field(default_factory=ShoppingSessionState)
    pending_app_events: list[str] = field(default_factory=list)


SESSIONS: dict[str, Session] = {}  # TODO(live): Redis or your app's session store
app = FastAPI(title="Trailhead Supply shopping agent")


class StartSession(BaseModel):
    user_id: str = Field(default="demo-user", max_length=64)  # TODO(live): from your auth


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    page: PageContext | None = None


def current(session_id: str | None) -> Session:
    if not session_id or session_id not in SESSIONS:
        raise HTTPException(401, "Unknown session")
    return SESSIONS[session_id]


def context(s: Session, page: PageContext | None = None) -> ShoppingSessionContext:
    return ShoppingSessionContext(
        session_id=s.session_id, user_id=s.user_id, page=page or PageContext(), now=datetime.now()
    )


@app.post("/api/session")
async def start_session(body: StartSession) -> dict:
    s = Session(session_id=secrets.token_urlsafe(24), user_id=body.user_id)
    SESSIONS[s.session_id] = s
    return {"session_id": s.session_id}


@app.post("/api/chat")
async def chat(body: ChatRequest, x_session_id: str | None = Header(default=None)):
    s = current(x_session_id)
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


@app.get("/api/cart")
async def get_cart(x_session_id: str | None = Header(default=None)) -> dict:
    s = current(x_session_id)
    return cart_payload(await backend.get_cart(context(s)))


@app.post("/webhooks/checkout-complete/{token}")
async def checkout_complete(token: str) -> dict:
    """Your payment provider calls this when the hosted checkout is paid.
    TODO(live): verify the provider's signature before trusting it."""
    session_id = backend.checkout_tokens.get(token, (None, None))[0]
    order = backend.record_paid_order(token)
    if order is None:
        raise HTTPException(404, "Unknown checkout")
    if session_id in SESSIONS:
        SESSIONS[session_id].pending_app_events.append(
            f"Customer completed payment; order {order.order_id} was placed."
        )
    return {"ok": True, "order_id": order.order_id}
