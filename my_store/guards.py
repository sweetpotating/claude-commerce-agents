"""Limits for a public deployment: the chat endpoint spends Anthropic tokens, so anyone who
finds it could run up the bill. In-memory, so they hold for one server process (one Render
instance); move them to Redis before running several.

Settings (environment), with defaults:
    CHAT_PER_MINUTE=10        messages per minute from one IP
    SESSIONS_PER_HOUR=20      new chats per hour from one IP
    TURNS_PER_SESSION=40      messages in one chat
    TURNS_PER_DAY=1500        messages across all shoppers per UTC day (the bill's ceiling)
    SESSION_IDLE_MINUTES=120  a chat untouched this long is dropped
    TRUST_PROXY=0             1 behind a proxy that appends the client to X-Forwarded-For
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from datetime import UTC, datetime

from fastapi import HTTPException, Request


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


CHAT_PER_MINUTE = _int("CHAT_PER_MINUTE", 10)
SESSIONS_PER_HOUR = _int("SESSIONS_PER_HOUR", 20)
TURNS_PER_SESSION = _int("TURNS_PER_SESSION", 40)
TURNS_PER_DAY = _int("TURNS_PER_DAY", 1500)
SESSION_IDLE_SECONDS = _int("SESSION_IDLE_MINUTES", 120) * 60
TRUST_PROXY = os.environ.get("TRUST_PROXY") == "1"


def client_ip(request: Request) -> str:
    """The shopper's IP. Behind a proxy (Render), the last X-Forwarded-For entry is the one
    the proxy itself added; earlier entries come from the client and can be forged."""
    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """At most ``limit`` hits per key in any ``window`` seconds."""

    def __init__(self, limit: int, window: float, clock=time.monotonic) -> None:
        self.limit, self.window, self._clock = limit, window, clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, message: str) -> None:
        now = self._clock()
        hits = self._hits[key]
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            raise HTTPException(429, message)
        hits.append(now)
        if len(self._hits) > 10_000:  # forget idle keys so the table stays small
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= self.window]:
                del self._hits[k]


class DailyBudget:
    def __init__(self, limit: int, today=lambda: datetime.now(UTC).date()) -> None:
        self.limit, self._today = limit, today
        self._day, self._used = today(), 0

    def spend(self) -> None:
        if self._today() != self._day:
            self._day, self._used = self._today(), 0
        if self._used >= self.limit:
            raise HTTPException(429, "The assistant is resting for today. Please try again tomorrow.")
        self._used += 1


chat_limiter = RateLimiter(CHAT_PER_MINUTE, 60)
session_limiter = RateLimiter(SESSIONS_PER_HOUR, 3600)
daily_budget = DailyBudget(TURNS_PER_DAY)
