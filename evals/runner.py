"""Run the eval suite against the app, in-process, on the live store.

    python -m evals.runner                       # every case, 1 trial, judged
    python -m evals.runner --tags conversion -n 3
    python -m evals.runner --ids discovery-001 --no-judge
    python -m evals.runner --update-baseline     # promote this run's results

Each case runs in a fresh chat through the real FastAPI app (my_store/app.py): the same
host code, gates, and Shopify backend a shopper's browser reaches, with the rate limits
lifted for the run. A case's preconditions (the page it opens on, products already seen,
the cart) are injected before its turns; persona cases are played by a simulated shopper
until it checks out or gives up. Output goes to evals/results/<stamp>/: results.jsonl,
traces/, summary.json, summary.md. Against evals/baseline.json the run lists newly failing
and newly passing cases; it exits 1 when a critical case fails or a baseline pass now fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CASES = ROOT / "cases"
RESULTS = ROOT / "results"
BASELINE = ROOT / "baseline.json"

# Prices per million tokens (input, output) for cost estimates; cache writes 1.25x input,
# cache reads 0.1x input.
PRICES = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def _prepare_env(backend: str) -> None:
    os.environ.setdefault("STORE_BACKEND", backend)
    os.environ["STORE_BACKEND"] = backend
    for key in (
        "CHAT_PER_MINUTE",
        "SESSIONS_PER_HOUR",
        "TURNS_PER_DAY",
        "TURNS_PER_IP_PER_DAY",
        "TURNS_PER_SESSION",
    ):
        os.environ[key] = "1000000"
    os.environ["TOKENS_PER_DAY"] = str(10**12)


def load_cases(ids: list[str] | None, tags: list[str] | None) -> list[dict]:
    cases = []
    for path in sorted(CASES.glob("*.json")):
        for case in json.loads(path.read_text()):
            case.setdefault("flow", path.stem)
            cases.append(case)
    if ids:
        cases = [c for c in cases if any(c["id"].startswith(i) for i in ids)]
    if tags:
        cases = [c for c in cases if set(tags) & set(c.get("tags", [])) or c["flow"] in tags]
    return [c for c in cases if not c.get("skip")]


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.split("\n\n"):
        event, data = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if event and data:
            try:
                events.append((event, json.loads(data)))
            except ValueError:
                pass
    return events


def turn_record(user: str, events: list[tuple[str, dict]], secs: float) -> dict:
    turn: dict[str, Any] = {
        "kind": "chat",
        "user": user,
        "text": "",
        "tools": [],
        "results": [],
        "ui": [],
        "cart": None,
        "error": None,
        "secs": round(secs, 2),
        "usage": {},
    }
    for event, d in events:
        if event == "text_delta":
            turn["text"] += d.get("text", "")
        elif event == "tool_call":
            turn["tools"].append((d.get("tool"), d.get("input") or {}))
        elif event == "tool_result":
            ok = not d.get("is_error") and d.get("status") != "blocked"
            turn["results"].append((d.get("tool"), ok, d.get("summary") or d.get("excerpt") or ""))
        elif event == "ui":
            turn["ui"].append((d.get("component"), d.get("payload") or {}))
        elif event == "cart_update":
            turn["cart"] = d.get("cart")
        elif event == "error":
            turn["error"] = d.get("message")
        elif event == "turn_complete":
            turn["usage"] = d.get("usage") or {}
    # A turn that errors before any model output is an outage (credit, rate limit, network),
    # not the agent's behaviour; the runner reports it apart and stops counting it.
    turn["infra_error"] = bool(turn["error"]) and not turn["usage"] and not turn["tools"]
    return turn


class Chat:
    """One chat session against the app, the way chat.html drives it."""

    def __init__(self, http, page: dict | None) -> None:
        self.http, self.page = http, page
        self.sid: str | None = None
        self.turns: list[dict] = []
        self.cart: dict | None = None

    @property
    def headers(self) -> dict:
        return {"x-session-id": self.sid or ""}

    async def start(self) -> None:
        self.sid = (await self.http.post("/api/session", json={})).json()["session_id"]
        if self.page:
            await self.http.post("/api/page", json=self.page, headers=self.headers)

    async def say(self, text: str, source: str | None = None) -> dict:
        started = time.monotonic()
        body = {"message": text, "source": source, "page": self.page}
        response = await self.http.post("/api/chat", json=body, headers=self.headers, timeout=400)
        if response.status_code != 200:
            turn = turn_record(text, [], time.monotonic() - started)
            turn["error"] = f"HTTP {response.status_code}: {response.text[:200]}"
        else:
            turn = turn_record(text, parse_sse(response.text), time.monotonic() - started)
        if turn["cart"] is not None:
            self.cart = turn["cart"]
        turn["cart"] = turn["cart"] if turn["cart"] is not None else self.cart
        self.turns.append(turn)
        return turn

    async def tap(self, label: str, path: str, body: dict | None = None) -> dict:
        """A card button: Add to cart, a variant, Checkout - no model turn."""
        started = time.monotonic()
        response = await self.http.post(path, json=body or {}, headers=self.headers)
        turn: dict[str, Any] = {
            "kind": "tap",
            "user": label,
            "text": "",
            "tools": [],
            "results": [],
            "ui": [],
            "cart": self.cart,
            "error": None,
            "secs": round(time.monotonic() - started, 2),
            "usage": {},
        }
        if response.status_code != 200:
            turn["error"] = f"{label}: HTTP {response.status_code} {response.text[:200]}"
        else:
            data = response.json()
            if path == "/api/checkout":
                turn["ui"].append(("checkout", data))
                self.cart = data.get("cart") or self.cart
            elif path == "/api/cart/add":
                self.cart = data
            turn["cart"] = self.cart
            turn["data"] = data
        self.turns.append(turn)
        return turn


async def inject_state(chat: Chat, state: dict, host) -> None:
    """Products already seen and the cart, before the case's own turns."""
    from shopping_agent import ShoppingSessionContext  # noqa: PLC0415

    session = host.SESSIONS[chat.sid]
    ctx = ShoppingSessionContext(session_id=chat.sid, user_id=session.user_id, now=datetime.now())
    for title in state.get("seen", []):
        found = await host.backend.search_products(ctx, title, limit=5)
        match = next((p for p in found if title.lower() in p.title.lower()), None)
        if match is None:
            raise RuntimeError(f"state: no product titled {title!r}")
        details = await host.backend.get_product_details(ctx, match.product_id)
        session.state.remember_products([details, *details.variants])
    for line in state.get("cart", []):
        product = next(
            p
            for p in session.state.seen_products.values()
            if line["title"].lower() in p.title.lower() and not p.variant_of
        )
        pid = product.product_id
        if line.get("option"):
            pid = next(
                p.product_id
                for p in session.state.seen_products.values()
                if p.variant_of == product.product_id and p.option_values == line["option"]
            )
        await chat.tap(
            f"Add {line['title']}", "/api/cart/add", {"product_id": pid, "quantity": line.get("qty", 1)}
        )
    chat.turns.clear()  # setup is not graded


def shopper_view(turns: list[dict]) -> str:
    """The conversation as the shopper saw it: words, cards with their buttons, chips, cart."""
    from .graders import products_in  # noqa: PLC0415

    lines = []
    for t in turns:
        if t["kind"] == "tap":
            lines.append(f"YOU TAPPED: {t['user']}" + (f" -> {t['error']}" if t.get("error") else ""))
            continue
        lines.append(f"YOU: {t['user']}")
        if t["text"]:
            lines.append(f"ASSISTANT: {t['text']}")
        for component, p in t["ui"]:
            if component == "suggestions":
                lines.append(f"CHIPS: {p.get('suggestions')}")
            elif component == "checkout":
                lines.append("CHECKOUT CARD shown with a 'Check out securely' button")
        products = products_in(t)
        for p in products[:10]:
            opts = f" options {p['options']}" if p.get("options") else ""
            price = f"{p.get('price')} {p.get('currency', '')}"
            lines.append(f"PRODUCT CARD: {p.get('title')} - {price}{opts} [Add to cart]")
        if t.get("error"):
            lines.append(f"ERROR SHOWN: {t['error']}")
    cart = next((t["cart"] for t in reversed(turns) if t.get("cart")), None)
    items = ", ".join(f"{i['title']} x{i['quantity']}" for i in (cart or {}).get("items", []))
    lines.append(f"YOUR CART: {items or 'empty'}")
    return "\n".join(lines)


async def run_persona(chat: Chat, case: dict) -> dict:
    from .graders import products_in  # noqa: PLC0415
    from .judge import shopper_step  # noqa: PLC0415

    persona = case["persona"]
    usage: list[dict] = []
    await chat.say(case["opening"])
    outcome = {"converted": False, "gave_up": False, "actions": []}
    for _ in range(case.get("max_turns", 6)):
        action, used = await shopper_step(persona, shopper_view(chat.turns))
        usage.append(used)
        if action is None:
            outcome["shopper_error"] = used.get("error")
            break
        outcome["actions"].append(f"{action.action}: {action.text}".strip(": "))
        if action.action == "give_up":
            outcome["gave_up"] = True
            break
        if action.action == "checkout":
            tap = await chat.tap("Checkout", "/api/checkout")
            card = tap.get("data") or {}
            if any(h.get("url") for h in card.get("handoffs", [])):
                outcome["converted"] = True
                break
            continue
        if action.action == "add_to_cart":
            shown = [p for t in chat.turns for p in products_in(t)]
            want = action.text.lower()
            product = next((p for p in shown if p.get("title", "").lower() == want), None) or next(
                (
                    p
                    for p in shown
                    if p.get("title", "").lower() in want or want in p.get("title", "").lower()
                ),
                None,
            )
            if product is None:
                await chat.say(f"Add {action.text} to my cart")  # no such card: the shopper types it
                continue
            pid = product["product_id"]
            if product.get("options"):
                opts = (
                    await chat.http.post(
                        "/api/product/options", json={"product_id": pid}, headers=chat.headers
                    )
                ).json()
                variants = [v for v in opts.get("variants", []) if v.get("in_stock")]
                chosen = next(
                    (v for v in variants if all(str(x).lower() in want for x in v["option_values"].values())),
                    variants[0] if variants else None,
                )
                pid = chosen["product_id"] if chosen else pid
            await chat.tap(
                f"Add to cart: {product['title']}", "/api/cart/add", {"product_id": pid, "quantity": 1}
            )
            continue
        await chat.say(action.text, source="chip" if action.action == "tap_chip" else None)
    outcome["shopper_usage"] = usage
    outcome["turns"] = len(chat.turns)
    return outcome


def _cost(usage: dict, model: str) -> float:
    price_in, price_out = PRICES.get(model, (2.0, 10.0))
    return (
        usage.get("input_tokens", 0) * price_in
        + usage.get("cache_creation_input_tokens", 0) * price_in * 1.25
        + usage.get("cache_read_input_tokens", 0) * price_in * 0.1
        + usage.get("output_tokens", 0) * price_out
    ) / 1e6


async def run_case(case: dict, trial: int, http, host, judge_on: bool) -> dict:
    from .graders import grade, products_in  # noqa: PLC0415
    from .judge import JUDGE_MODEL, SHOPPER_MODEL, judge  # noqa: PLC0415

    chat = Chat(http, case.get("state", {}).get("page"))
    started = time.monotonic()
    row: dict[str, Any] = {
        "id": case["id"],
        "flow": case["flow"],
        "priority": case.get("priority", "medium"),
        "tags": case.get("tags", []),
        "trial": trial,
    }
    try:
        await chat.start()
        await inject_state(chat, case.get("state", {}), host)
        if "persona" in case:
            row["conversion"] = await run_persona(chat, case)
        for text in case.get("turns", []):
            await chat.say(text)
        checks = grade(case, chat.turns)
        if "persona" in case:
            conv = row["conversion"]
            goal = [g.lower() for g in case.get("goal_items", [])]
            titles = [i["title"].lower() for i in (chat.cart or {}).get("items", [])]
            conv["goal_met"] = conv["converted"] and all(any(g in t for t in titles) for g in goal)
            checks["converted_with_goal"] = (
                conv["goal_met"],
                f"converted {conv['converted']}, cart {titles}, goal {goal}",
            )
        row["status"] = "ok"
    except Exception as error:
        checks = {"run": (False, f"run error: {error!r}")}
        row["status"] = "error"
    if any(t.get("infra_error") for t in chat.turns):
        row["status"] = "infra_error"
    row["checks"] = {k: {"passed": ok, "detail": detail} for k, (ok, detail) in checks.items()}
    if judge_on and case.get("expected", {}).get("rubric") and row["status"] == "ok":
        row["judge"] = await judge(case["expected"]["rubric"], chat.turns)
    judged = row.get("judge", {}).get("passed")
    row["passed"] = all(c["passed"] for c in row["checks"].values()) and judged is not False
    row["judge_error"] = "judge" in row and judged is None
    chat_turns = [t for t in chat.turns if t["kind"] == "chat"]
    row["latency_s"] = [t["secs"] for t in chat_turns]
    row["elapsed_s"] = round(time.monotonic() - started, 1)
    first_products = next((i for i, t in enumerate(chat_turns) if products_in(t)), None)
    row["first_product_turn"] = first_products + 1 if first_products is not None else None
    agent_model = host.config.model
    row["cost_usd"] = round(
        sum(_cost(t.get("usage") or {}, agent_model) for t in chat.turns)
        + _cost(row.get("judge", {}).get("usage") or {}, JUDGE_MODEL)
        + sum(_cost(u, SHOPPER_MODEL) for u in row.get("conversion", {}).get("shopper_usage", [])),
        4,
    )
    row["trace"] = chat.turns
    return row


def summarize(rows: list[dict], cases: list[dict]) -> dict:
    infra = [r for r in rows if r["status"] == "infra_error"]
    rows = [r for r in rows if r["status"] != "infra_error"]
    by_case: dict[str, list[dict]] = {}
    for r in rows:
        by_case.setdefault(r["id"], []).append(r)
    case_pass = {cid: sum(r["passed"] for r in rs) / len(rs) for cid, rs in by_case.items()}
    priority = {c["id"]: c.get("priority", "medium") for c in cases}

    def rate(ids):
        ids = list(ids)
        return round(sum(case_pass[i] for i in ids) / len(ids), 3) if ids else None

    flows: dict[str, list[str]] = {}
    for c in cases:
        flows.setdefault(c["flow"], []).append(c["id"])
    personas = [r for r in rows if "conversion" in r]
    latencies = [s for r in rows for s in r["latency_s"]]
    first_reply = [r for r in rows if "first_reply" in r["tags"]]
    return {
        "infra_errors": len(infra),
        "cases": len(by_case),
        "trials": len(rows),
        "pass_rate": rate(case_pass),
        "critical_pass_rate": rate(i for i in case_pass if priority.get(i) == "critical"),
        "by_flow": {f: rate(ids) for f, ids in sorted(flows.items()) if all(i in case_pass for i in ids)},
        "conversion": {
            "personas": len(personas),
            "checkout_reached": round(sum(r["conversion"]["converted"] for r in personas) / len(personas), 3)
            if personas
            else None,
            "goal_met": round(sum(bool(r["conversion"].get("goal_met")) for r in personas) / len(personas), 3)
            if personas
            else None,
            "turns_to_checkout_median": statistics.median(
                [r["conversion"]["turns"] for r in personas if r["conversion"]["converted"]]
            )
            if any(r["conversion"]["converted"] for r in personas)
            else None,
        },
        "first_reply_products_rate": round(
            sum(r["first_product_turn"] == 1 for r in first_reply) / len(first_reply), 3
        )
        if first_reply
        else None,
        "latency_s": {
            "median": round(statistics.median(latencies), 1) if latencies else None,
            "p90": round(sorted(latencies)[int(0.9 * (len(latencies) - 1))], 1) if latencies else None,
        },
        "judge_errors": sum(r.get("judge_error", False) for r in rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 2),
        "case_pass": case_pass,
        "failing": sorted(i for i, p in case_pass.items() if p < 0.5),
        "critical_failing": sorted(
            i for i, p in case_pass.items() if p < 1 and priority.get(i) == "critical"
        ),
    }


def compare(summary: dict) -> dict:
    if not BASELINE.exists():
        return {"baseline": None}
    base = json.loads(BASELINE.read_text())
    before = base.get("case_pass", {})
    now = summary["case_pass"]
    return {
        "baseline": base.get("stamp"),
        "newly_failing": sorted(i for i, p in now.items() if p < 0.5 <= before.get(i, 0)),
        "newly_passing": sorted(i for i, p in now.items() if p >= 0.5 > before.get(i, 1)),
        "pass_rate_delta": round(summary["pass_rate"] - base["pass_rate"], 3)
        if None not in (base.get("pass_rate"), summary["pass_rate"])
        else None,
    }


def write_markdown(stamp: str, summary: dict, diff: dict, rows: list[dict], path: Path) -> None:
    conv = summary["conversion"]
    pct = lambda v: "n/a" if v is None else f"{v:.0%}"  # noqa: E731
    lat = summary["latency_s"]
    lines = [
        f"# Eval run {stamp}",
        "",
        f"- Cases: {summary['cases']} ({summary['trials']} trials), "
        f"pass rate **{pct(summary['pass_rate'])}**, critical {pct(summary['critical_pass_rate'])}",
        f"- Conversion (simulated shoppers): checkout reached {pct(conv['checkout_reached'])}, "
        f"goal met {pct(conv['goal_met'])}, median turns to checkout {conv['turns_to_checkout_median']}",
        f"- First reply shows products: {pct(summary['first_reply_products_rate'])}",
        f"- Latency per turn: median {lat['median']}s, p90 {lat['p90']}s",
        f"- Cost: ${summary['cost_usd']} (agent + judge + shopper); judge errors: {summary['judge_errors']}",
    ]
    if diff.get("baseline"):
        failing, passing = diff["newly_failing"] or "none", diff["newly_passing"] or "none"
        lines.append(
            f"- Against baseline {diff['baseline']}: pass rate {pct(diff['pass_rate_delta'])} change, "
            f"newly failing {failing}, newly passing {passing}"
        )
    lines += ["", "| Flow | Pass rate |", "|---|---|"]
    lines += [f"| {f} | {r:.0%} |" for f, r in summary["by_flow"].items() if r is not None]
    lines += ["", "## Failures", ""]
    for r in rows:
        if r["passed"]:
            continue
        bad = [f"{k}: {v['detail']}" for k, v in r["checks"].items() if not v["passed"]]
        if r.get("judge") and r["judge"].get("passed") is False:
            bad.append(f"rubric: {r['judge']['reason']}")
        lines.append(f"- **{r['id']}** (trial {r['trial']}, {r['priority']}): " + " | ".join(bad)[:900])
    path.write_text("\n".join(lines) + "\n")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--tags", nargs="*")
    parser.add_argument("-n", "--trials", type=int, default=1)
    parser.add_argument("-j", "--concurrency", type=int, default=4)
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--backend", default=os.environ.get("EVAL_BACKEND", "shopify"))
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args(argv)

    _prepare_env(args.backend)
    sys.path.insert(0, str(ROOT.parent))
    import httpx  # noqa: PLC0415

    from my_store import app as host  # noqa: PLC0415  (reads the env above at import)

    cases = load_cases(args.ids, args.tags)
    if not cases:
        print("no cases match")
        return 2
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS / stamp
    (out / "traces").mkdir(parents=True, exist_ok=True)
    gate = asyncio.Semaphore(args.concurrency)
    transport = httpx.ASGITransport(app=host.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://evals", timeout=400) as http:

        async def one(case, trial):
            async with gate:
                row = await run_case(case, trial, http, host, not args.no_judge)
                mark = "PASS" if row["passed"] else "FAIL"
                print(f"[{mark}] {case['id']} t{trial} {row['elapsed_s']}s ${row['cost_usd']}", flush=True)
                return row

        rows = await asyncio.gather(*(one(c, t) for c in cases for t in range(args.trials)))

    with (out / "results.jsonl").open("w") as f:
        for row in rows:
            trace = row.pop("trace")
            (out / "traces" / f"{row['id']}_t{row['trial']}.json").write_text(
                json.dumps(trace, indent=1, default=str)
            )
            f.write(json.dumps(row, default=str) + "\n")
    summary = summarize(list(rows), cases)
    summary["stamp"] = stamp
    diff = compare(summary)
    (out / "summary.json").write_text(json.dumps({**summary, "diff": diff}, indent=1))
    write_markdown(stamp, summary, diff, list(rows), out / "summary.md")
    (RESULTS / "latest.md").write_text((out / "summary.md").read_text())
    print("\n" + (out / "summary.md").read_text())
    if args.update_baseline and not summary["infra_errors"]:
        BASELINE.write_text(json.dumps(summary, indent=1) + "\n")
        print(f"baseline updated to {stamp}")
    if summary["infra_errors"]:
        print(
            f"\n!! {summary['infra_errors']} trials hit an outage (model or store unreachable, e.g. "
            "out of API credit) and were left out of every number above. Fix that and re-run."
        )
        if args.update_baseline:
            print("baseline NOT updated: the run is incomplete")
        return 3
    regressions = diff.get("newly_failing") or []
    return 1 if summary["critical_failing"] or regressions else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
