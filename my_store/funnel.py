"""The conversion funnel, measured live: how many chats reach each step toward a sale.

    chat_started -> products_shown -> added_to_cart -> checkout_shown -> checkout_clicked

A chat counts once per step however often it repeats it, so the rates read as "of the
chats that saw products, how many added to cart". The server records every step it can
see; the checkout click happens in the shopper's browser, which reports it (POST
/api/event). GET /api/metrics returns the counts and step-to-step rates.

Each event is also logged as one JSON line ("funnel {...}"), so a log drain or Render's log
search keeps the history; the counts themselves live in memory and restart with the server
(the same trade-off as guards.py). Order completion happens on Shopify's checkout; match
checkout_clicked against Shopify's orders (utm_source on the checkout link,
CHECKOUT_UTM_SOURCE) for the last step.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from datetime import UTC, datetime

logger = logging.getLogger("funnel")
if not logger.handlers:  # uvicorn configures only its own loggers; these lines must reach the log
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

STEPS = ("chat_started", "products_shown", "added_to_cart", "checkout_shown", "checkout_clicked")
# Components that put products in front of the shopper.
PRODUCT_COMPONENTS = {"products", "comparison", "plan", "itinerary", "plan_matrix", "guide"}
# Steps a browser may report (the rest are seen by the server itself).
CLIENT_EVENTS = {"checkout_clicked", "product_opened"}


class Funnel:
    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self.started_at = clock()
        self._sessions: dict[str, set[str]] = defaultdict(set)
        self.events: dict[str, int] = defaultdict(int)

    def record(self, session_id: str, step: str, **detail: object) -> None:
        self.events[step] += 1
        first = step not in self._sessions[session_id]
        self._sessions[session_id].add(step)
        line = {"step": step, "session": session_id[:8], "first": first, **detail}
        line["at"] = datetime.fromtimestamp(self._clock(), UTC).isoformat(timespec="seconds")
        logger.info("funnel %s", json.dumps(line, default=str))

    def report(self) -> dict:
        counts = {step: sum(step in s for s in self._sessions.values()) for step in STEPS}
        rates = {
            f"{a}->{b}": round(counts[b] / counts[a], 3) if counts[a] else None
            for a, b in zip(STEPS, STEPS[1:], strict=False)
        }
        started = counts["chat_started"]
        return {
            "since": datetime.fromtimestamp(self.started_at, UTC).isoformat(timespec="seconds"),
            "chats": counts,
            "step_rates": rates,
            "chat_to_cart": round(counts["added_to_cart"] / started, 3) if started else None,
            "chat_to_checkout_click": round(counts["checkout_clicked"] / started, 3) if started else None,
            "events": dict(self.events),
        }


funnel = Funnel()
