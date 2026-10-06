"""The real app on the live store with the scripted model (e2e/scripted_model.py) in place
of Claude, for checking the whole bot from a shopper's seat without an API call.

    python -m scripts.e2e_server            # http://localhost:8000
"""

from __future__ import annotations

import os

import uvicorn

os.environ.setdefault("STORE_BACKEND", "shopify")
os.environ.setdefault("ANTHROPIC_API_KEY", "scripted-no-api-calls")
for _limit in ("CHAT_PER_MINUTE", "SESSIONS_PER_HOUR", "TURNS_PER_IP_PER_DAY"):  # one tester, many runs
    os.environ.setdefault(_limit, "100000")

from e2e.scripted_model import ScriptedClient  # noqa: E402
from my_store import app as host  # noqa: E402

host.agent.client = ScriptedClient()  # every model call is scripted; nothing reaches Anthropic


@host.app.post("/e2e/outage/{on}", include_in_schema=False)
async def outage(on: int) -> dict:
    """Test switch: make every model call fail like an out-of-credit account."""
    host.agent.client.outage = bool(on)
    return {"outage": host.agent.client.outage}


if __name__ == "__main__":
    uvicorn.run(host.app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
