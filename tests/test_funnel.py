"""The live conversion funnel: each chat counts once per step, the browser can report only
its own steps, and the metrics endpoint exists only with a token."""

from __future__ import annotations

from fastapi.testclient import TestClient

from my_store import app as host
from my_store.funnel import Funnel


def test_a_chat_counts_once_per_step_and_rates_follow_the_steps():
    f = Funnel(clock=lambda: 0)
    for sid in ("a", "b", "c", "d"):
        f.record(sid, "chat_started")
    for sid in ("a", "b"):
        f.record(sid, "products_shown")
        f.record(sid, "products_shown")  # a second card in the same chat counts once
    f.record("a", "added_to_cart")
    report = f.report()
    assert report["chats"]["products_shown"] == 2 and report["events"]["products_shown"] == 4
    assert report["step_rates"]["chat_started->products_shown"] == 0.5
    assert report["step_rates"]["products_shown->added_to_cart"] == 0.5
    assert report["chat_to_cart"] == 0.25
    assert report["step_rates"]["added_to_cart->checkout_shown"] == 0.0


def test_card_adds_and_checkout_clicks_reach_the_funnel(monkeypatch):
    f = Funnel()
    monkeypatch.setattr(host, "funnel", f)
    monkeypatch.setenv("METRICS_TOKEN", "s3cret")
    client = TestClient(host.app)
    sid = client.post("/api/session", json={}).json()["session_id"]
    h = {"x-session-id": sid}
    client.post("/api/page", json={"page_type": "product", "product_id": "TS-200"}, headers=h)
    assert client.post("/api/cart/add", json={"product_id": "TS-200"}, headers=h).status_code == 200
    assert client.post("/api/checkout", headers=h).status_code == 200
    assert client.post("/api/event", json={"session_id": sid, "name": "checkout_clicked"}).status_code == 204
    # A step the browser may not claim, and an unknown chat, are ignored.
    client.post("/api/event", json={"session_id": sid, "name": "added_to_cart"})
    client.post("/api/event", json={"session_id": "nope", "name": "checkout_clicked"})

    chats = client.get("/api/metrics", headers={"x-metrics-token": "s3cret"}).json()["chats"]
    assert chats["products_shown"] == chats["added_to_cart"] == chats["checkout_shown"] == 1
    assert chats["checkout_clicked"] == 1 and f.events["added_to_cart"] == 1
    assert client.get("/api/metrics?token=wrong").status_code == 404


def test_metrics_do_not_exist_without_a_token(monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    assert TestClient(host.app).get("/api/metrics?token=").status_code == 404
