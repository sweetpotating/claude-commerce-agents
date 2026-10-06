"""Limits for a public deployment: the chat endpoint spends Anthropic tokens, so anyone who
finds it could run up the bill. In-memory, so they hold for one server process (one Render
instance); move them to Redis before running several.

Settings (environment), with defaults:
    CHAT_PER_MINUTE=10        messages per minute from one IP
    SESSIONS_PER_HOUR=60      new chats per hour from one IP
    TURNS_PER_SESSION=40      messages in one chat
    TURNS_PER_DAY=1500        messages across all shoppers per UTC day
    TURNS_PER_IP_PER_DAY=150  messages from one IP per UTC day, so one visitor can't use the day up
    TOKENS_PER_DAY=10000000   tokens across all shoppers per UTC day, weighted to input-token
                              cost (output x5, cache writes x1.25, cache reads x0.1): the
                              bill's ceiling, ~US$30/day at US$3 per million input tokens
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
# 60, not 20: a mobile carrier or an office puts many shoppers behind one IP.
SESSIONS_PER_HOUR = _int("SESSIONS_PER_HOUR", 60)
TURNS_PER_SESSION = _int("TURNS_PER_SESSION", 40)
TURNS_PER_DAY = _int("TURNS_PER_DAY", 1500)
TURNS_PER_IP_PER_DAY = _int("TURNS_PER_IP_PER_DAY", 150)
TOKENS_PER_DAY = _int("TOKENS_PER_DAY", 10_000_000)
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
            # When the oldest hit leaves the window, a new one is allowed (eval item 37).
            wait = max(1, int(self.window - (now - hits[0])) + 1)
            raise HTTPException(429, message, headers={"Retry-After": str(wait)})
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


class TokenBudget:
    """Model usage per UTC day, weighted to what it costs. Checked before a turn starts and
    charged with the turn's real usage when it ends, so one long turn can overrun it once,
    then new turns wait for the next day.

    Like every limit here it lives in this process: a restart (Render's free plan sleeps
    when idle) starts the day again. Set a spend limit on the API key's workspace in the
    Anthropic Console as the ceiling that survives restarts (DEPLOY.md)."""

    WEIGHTS = {
        "input_tokens": 1.0,
        "cache_creation_input_tokens": 1.25,
        "cache_read_input_tokens": 0.1,
        "output_tokens": 5.0,
    }

    def __init__(self, limit: int, today=lambda: datetime.now(UTC).date()) -> None:
        self.limit, self._today = limit, today
        self._day, self.used = today(), 0.0

    def _roll(self) -> None:
        if self._today() != self._day:
            self._day, self.used = self._today(), 0.0

    def check(self) -> None:
        self._roll()
        if self.used >= self.limit:
            raise HTTPException(429, "The assistant is resting for today. Please try again tomorrow.")

    def charge(self, usage: dict) -> None:
        self._roll()
        self.used += sum(self.WEIGHTS.get(k, 0) * (v or 0) for k, v in (usage or {}).items())


class DailyPerKey:
    """At most ``limit`` hits per key per UTC day."""

    def __init__(self, limit: int, today=lambda: datetime.now(UTC).date()) -> None:
        self.limit, self._today = limit, today
        self._day, self._counts = today(), defaultdict(int)

    def check(self, key: str, message: str) -> None:
        if self._today() != self._day:
            self._day, self._counts = self._today(), defaultdict(int)
        if self._counts[key] >= self.limit:
            raise HTTPException(429, message)
        self._counts[key] += 1


chat_limiter = RateLimiter(CHAT_PER_MINUTE, 60)
session_limiter = RateLimiter(SESSIONS_PER_HOUR, 3600)
daily_budget = DailyBudget(TURNS_PER_DAY)
ip_daily = DailyPerKey(TURNS_PER_IP_PER_DAY)
token_budget = TokenBudget(TOKENS_PER_DAY)
