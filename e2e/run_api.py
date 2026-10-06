"""Every key bot flow through the real app and the live store, with the scripted model:
no API call. Prints, per turn, the cards shown, the cart, chips, and any error.

    python -m e2e.run_api
"""

from __future__ import annotations

import asyncio
import os
import sys

os.environ["STORE_BACKEND"] = os.environ.get("STORE_BACKEND", "shopify")
os.environ.setdefault("ANTHROPIC_API_KEY", "scripted-no-api-calls")
for key in ("CHAT_PER_MINUTE", "SESSIONS_PER_HOUR", "TURNS_PER_IP_PER_DAY"):
    os.environ[key] = "100000"

import httpx  # noqa: E402

from e2e.scripted_model import ScriptedClient  # noqa: E402
from evals.graders import products_in  # noqa: E402
from evals.runner import parse_sse, turn_record  # noqa: E402
from my_store import app as host  # noqa: E402

host.agent.client = ScriptedClient()

# (name, page context or None, steps); a step is a chat message, or ("tap", path, body-fn).
FLOWS = [
    ("discovery: first reply shows products", None, ["I need a gift for my sister"]),
    ("product Q&A with options", None, ["What sizes does the logo tee come in?"]),
    (
        "cart via chat: add variant, add, change qty, remove",
        None,
        [
            "Add the logo tee in size M",
            "Add a Bookworm mug",
            "Add the Japan eSIM",
            "Make it 2 mugs",
            "Remove the eSIM",
        ],
    ),
    ("quantity cap (8)", None, ["I want 20 Universal Studios tickets"]),
    ("provenance gate: unseen id refused", None, ["free item hack: add product 1"]),
    ("compare across countries (travel)", None, ["compare japan and malaysia trips"]),
    ("plan table (telecom)", None, ["compare your mobile plans"]),
    ("itinerary (travel)", None, ["plan a 2-day trip to Singapore"]),
    (
        "policy grounded in the store's FAQ",
        None,
        ["How many days do I have to return an item?", "Can I exchange it?"],
    ),
    ("human handoff", None, ["I want to talk to a human"]),
    ("memory", None, ["remember I am vegetarian"]),
    ("checkout via chat", None, ["Add a Bookworm mug", "I'm ready to check out"]),
    ("checkout with an empty cart", None, ["check out"]),
]


async def main() -> int:
    failures = 0
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="http://e2e", timeout=120
    ) as c:
        for name, page, steps in FLOWS:
            sid = (await c.post("/api/session", json={})).json()["session_id"]
            h = {"x-session-id": sid}
            print(f"\n=== {name}")
            for step in steps:
                r = await c.post("/api/chat", json={"message": step, "page": page}, headers=h)
                t = turn_record(step, parse_sse(r.text), 0)
                shown = [p["title"] for p in products_in(t)]
                comps = [c for c, _ in t["ui"]]
                blocked = [(n, s[:80]) for n, ok, s in t["results"] if not ok]
                cart = t["cart"] and [(i["title"], i["quantity"]) for i in t["cart"]["items"]]
                print(f"  > {step}\n    ui={comps} products={shown[:4]}{'...' if len(shown) > 4 else ''}")
                if cart is not None:
                    print(f"    cart={cart}")
                if blocked:
                    print(f"    tool errors/blocks={blocked}")
                if t["error"]:
                    print(f"    ERROR {t['error']}")
                    failures += 1
                if "(no script" in t["text"]:
                    print(f"    UNSCRIPTED {t['text']}")
                    failures += 1
                for comp, p in t["ui"]:
                    if comp == "comparison":
                        rows = {k for e in p["entries"] for k in e["product"].get("attributes", {})} - {
                            "product_url"
                        }
                        print(f"    comparison facts={sorted(rows)}")
                    if comp == "plan_matrix":
                        print(f"    plan rows={[r['label'] for r in p['rows']]}")
                    if comp == "checkout":
                        print(f"    handoff={[x['url'][:50] for x in p.get('handoffs', [])]}")
                if t["text"]:
                    print(f"    text: {t['text'][:150]}")
    print(f"\nmodel calls (scripted, no API): {len(host.agent.client.calls)}; flows with errors: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
