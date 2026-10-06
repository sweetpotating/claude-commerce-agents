"""The public-launch limits in my_store/guards.py and the endpoints the widget uses."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from my_store import guards


def request(forwarded: str | None, peer: str = "10.0.0.1") -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


def test_rate_limiter_allows_the_limit_then_refuses_until_the_window_passes():
    now = [0.0]
    limiter = guards.RateLimiter(2, 60, clock=lambda: now[0])
    limiter.check("ip", "slow down")
    limiter.check("ip", "slow down")
    with pytest.raises(HTTPException) as refused:
        limiter.check("ip", "slow down")
    assert refused.value.status_code == 429 and refused.value.detail == "slow down"
    limiter.check("other-ip", "slow down")  # keys are separate
    now[0] = 60.0
    limiter.check("ip", "slow down")


def test_daily_budget_resets_on_a_new_day():
    day = [date(2026, 10, 6)]
    budget = guards.DailyBudget(1, today=lambda: day[0])
    budget.spend()
    with pytest.raises(HTTPException):
        budget.spend()
    day[0] = date(2026, 10, 7)
    budget.spend()


def test_client_ip_takes_the_hop_the_proxy_added(monkeypatch):
    monkeypatch.setattr(guards, "TRUST_PROXY", True)
    # The first entry is whatever the client claimed; the last is what the proxy saw.
    assert guards.client_ip(request("6.6.6.6, 203.0.113.7")) == "203.0.113.7"
    assert guards.client_ip(request(None)) == "10.0.0.1"
    monkeypatch.setattr(guards, "TRUST_PROXY", False)
    assert guards.client_ip(request("203.0.113.7")) == "10.0.0.1"


def test_app_serves_the_widget_and_history():
    from fastapi.testclient import TestClient

    from my_store import app as host

    client = TestClient(host.app)
    assert client.get("/healthz").json() == {"ok": True}
    assert "sa-bubble" in client.get("/widget.js").text
    sid = client.post("/api/session", json={}).json()["session_id"]
    host.SESSIONS[sid].messages += [
        {"role": "user", "content": "a tent"},
        {"role": "assistant", "content": [{"type": "tool_use"}, {"type": "text", "text": "Here are two."}]},
    ]
    turns = client.get("/api/history", headers={"x-session-id": sid}).json()["turns"]
    assert turns == [{"role": "user", "text": "a tent"}, {"role": "assistant", "text": "Here are two."}]
    assert client.get("/api/history", headers={"x-session-id": "nope"}).status_code == 401


def test_each_anonymous_chat_gets_its_own_memory_owner():
    # Live UAT: every chat was "demo-user", so one shopper's saved facts reached the next.
    from fastapi.testclient import TestClient

    from my_store import app as host

    client = TestClient(host.app)
    first = client.post("/api/session", json={}).json()["session_id"]
    second = client.post("/api/session", json={"user_id": host.SESSIONS[first].user_id}).json()["session_id"]
    owners = {host.SESSIONS[first].user_id, host.SESSIONS[second].user_id}
    assert len(owners) == 2 and "demo-user" not in owners


def test_the_token_budget_charges_real_usage_by_cost_and_resets_daily():
    day = [date(2026, 10, 6)]
    budget = guards.TokenBudget(1000, today=lambda: day[0])
    budget.check()
    budget.charge({"input_tokens": 400, "output_tokens": 100, "cache_read_input_tokens": 1000})
    assert budget.used == 400 + 500 + 100
    with pytest.raises(HTTPException):
        budget.check()
    day[0] = date(2026, 10, 7)
    budget.check()


def test_one_ip_cannot_use_up_the_day():
    per_ip = guards.DailyPerKey(2)
    per_ip.check("1.2.3.4", "limit")
    per_ip.check("1.2.3.4", "limit")
    with pytest.raises(HTTPException):
        per_ip.check("1.2.3.4", "limit")
    per_ip.check("5.6.7.8", "limit")  # another visitor is unaffected


def test_the_widget_is_told_which_stores_it_serves(monkeypatch):
    from fastapi.testclient import TestClient

    from my_store import app as host

    monkeypatch.setattr(host, "WIDGET_SHOPS", ["iknowledge-dev.myshopify.com"])
    client = TestClient(host.app)
    config = client.get("/api/widget-config")
    assert config.json() == {"shops": ["iknowledge-dev.myshopify.com"]}
    assert config.headers["access-control-allow-origin"] == "*"  # read from the store's domain
    script = client.get("/widget.js").text
    assert "/api/widget-config" in script and "Shopify.shop" in script
