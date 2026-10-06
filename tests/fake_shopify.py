"""An in-process stand-in for one Shopify store's UCP and Storefront MCP endpoints, shaped
after the request and response examples on shopify.dev/docs/agents. It lets the backend
run end to end without network access; it is not a full UCP implementation."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

SHOP = "trailhead-demo.myshopify.com"

_VARIANTS = {
    "gid://shopify/ProductVariant/101": ("gid://shopify/Product/1", {"Size": "S"}, 4800, True),
    "gid://shopify/ProductVariant/102": ("gid://shopify/Product/1", {"Size": "M"}, 4800, False),
    "gid://shopify/ProductVariant/103": ("gid://shopify/Product/1", {"Size": "L"}, 5200, True),
    "gid://shopify/ProductVariant/201": ("gid://shopify/Product/2", {"Title": "Default Title"}, 22900, True),
}
_PRODUCTS = {
    "gid://shopify/Product/1": {
        "title": "Ridgeline Merino Tee",
        "description": {"html": "<p>Lightweight <b>merino</b> tee.</p>"},
        "options": {"Size": ["S", "M", "L"]},
    },
    "gid://shopify/Product/2": {
        "title": "Summit 2P Backpacking Tent",
        "description": {"html": "Freestanding two-person tent."},
        "options": {"Title": ["Default Title"]},
    },
}


def _variant(vid: str) -> dict[str, Any]:
    product_id, values, amount, available = _VARIANTS[vid]
    return {
        "id": vid,
        "title": " / ".join(values.values()),
        "price": {"amount": amount, "currency": "USD"},
        "availability": {"available": available, "status": "in_stock" if available else "out_of_stock"},
        "options": [{"name": n, "label": label} for n, label in values.items()],
        "seller": {"name": "Trailhead", "domain": SHOP},
    }


def _product(pid: str, variants: list[str]) -> dict[str, Any]:
    spec = _PRODUCTS[pid]
    amounts = [a for v, (p, _, a, _) in _VARIANTS.items() if p == pid]
    return {
        "id": pid,
        "title": spec["title"],
        "description": spec["description"],
        "options": [
            {
                "name": name,
                "values": [
                    {
                        "label": label,
                        "exists": True,
                        "available": any(
                            avail
                            for p, vals, _, avail in _VARIANTS.values()
                            if p == pid and vals.get(name) == label
                        ),
                    }
                    for label in labels
                ],
            }
            for name, labels in spec["options"].items()
        ],
        "price_range": {
            "min": {"amount": min(amounts), "currency": "USD"},
            "max": {"amount": max(amounts), "currency": "USD"},
        },
        "variants": [_variant(v) for v in variants],
    }


class FakeShopifyStore:
    def __init__(self) -> None:
        self.carts: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if request.url.host == "api.shopify.com":
            return httpx.Response(200, json={"access_token": "header.payload.sig"})
        name = body["params"]["name"]
        args = body["params"]["arguments"]
        self.calls.append((name, args, dict(request.headers)))
        if request.url.path == "/api/ucp/mcp" and "ucp-agent" not in args.get("meta", {}):
            return self._result(body, {"messages": [{"type": "error", "code": "profile_required"}]}, True)
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601}})
        return handler(body, args, request)

    @staticmethod
    def _result(body: dict[str, Any], content: dict[str, Any], is_error: bool = False) -> httpx.Response:
        result: dict[str, Any] = {"structuredContent": content}
        if is_error:
            result["isError"] = True
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    # -- catalog -----------------------------------------------------------------------

    def _search_catalog(self, body, args, request):
        words = [w for w in args["catalog"]["query"].lower().split() if len(w) > 2]
        hits = [
            {k: v for k, v in _product(pid, []).items() if k != "variants"}
            for pid, spec in _PRODUCTS.items()
            if any(w in spec["title"].lower() for w in words)
        ]
        price = args["catalog"].get("filters", {}).get("price", {})
        if "max" in price:
            hits = [h for h in hits if h["price_range"]["min"]["amount"] <= price["max"]]
        return self._result(body, {"products": hits})

    def _get_product(self, body, args, request):
        pid = args["catalog"]["id"]
        if pid in _VARIANTS:  # a variant id resolves to its product with that variant
            return self._result(body, {"product": _product(_VARIANTS[pid][0], [pid])})
        if pid not in _PRODUCTS:
            return self._result(body, {"messages": [{"type": "error", "code": "not_found"}]}, True)
        selected = {s["name"]: s["label"] for s in args["catalog"].get("selected", [])}
        candidates = [v for v, (p, vals, _, _) in _VARIANTS.items() if p == pid]
        matching = [
            v for v in candidates if all(_VARIANTS[v][1].get(n) == lab for n, lab in selected.items())
        ]
        # Like Shopify: one variant comes back, the first match (default selection if none).
        return self._result(body, {"product": _product(pid, matching[:1] or candidates[:1])})

    # -- cart --------------------------------------------------------------------------

    def _render_cart(self, cart_id: str, lines: list[dict[str, Any]], messages: list[dict]) -> dict:
        items = []
        for line in lines:
            vid = line["item"]["id"]
            pid, _, amount, _ = _VARIANTS[vid]
            items.append(
                {
                    "id": f"li_{vid[-3:]}",
                    "item": {"id": vid, "title": _PRODUCTS[pid]["title"], "price": amount},
                    "quantity": line["quantity"],
                    "totals": [{"type": "subtotal", "amount": amount * line["quantity"]}],
                }
            )
        total = sum(i["totals"][0]["amount"] for i in items)
        cart = {
            "id": cart_id,
            "currency": "USD",
            "line_items": items,
            "totals": [{"type": "subtotal", "amount": total}, {"type": "total", "amount": total}],
            "continue_url": f"https://{SHOP}/cart/c/{cart_id.split('/')[-1]}",
            "messages": messages,
        }
        self.carts[cart_id] = cart
        return {"cart": cart}

    def _accept(self, lines: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
        kept, messages = [], []
        for line in lines:
            if _VARIANTS[line["item"]["id"]][3]:
                kept.append(line)
            else:
                messages.append({"type": "error", "code": "out_of_stock", "severity": "recoverable"})
        return kept, messages

    def _create_cart(self, body, args, request):
        kept, messages = self._accept(args["cart"]["line_items"])
        cart_id = f"gid://shopify/Cart/{uuid.uuid4().hex[:8]}"
        return self._result(body, self._render_cart(cart_id, kept, messages))

    def _update_cart(self, body, args, request):
        kept, messages = self._accept(args["cart"]["line_items"])
        return self._result(body, self._render_cart(args["id"], kept, messages))

    def _get_cart(self, body, args, request):
        cart = self.carts.get(args["id"])
        if cart is None:
            return self._result(body, {"cart": {"messages": [{"type": "error", "code": "not_found"}]}})
        return self._result(body, {"cart": cart})

    def _cancel_cart(self, body, args, request):
        self.carts.pop(args["id"], None)
        return self._result(body, {"cart": {"id": args["id"], "status": "canceled"}})

    # -- checkout and policies ------------------------------------------------------------

    def _create_checkout(self, body, args, request):
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return self._result(body, {"messages": [{"type": "error", "code": "unauthorized"}]}, True)
        token = uuid.uuid4().hex[:8]
        return self._result(
            body,
            {
                "checkout": {
                    "id": f"gid://shopify/Checkout/{token}",
                    "status": "incomplete",
                    "continue_url": f"https://{SHOP}/checkouts/cn/{token}",
                }
            },
        )

    def _search_shop_policies_and_faqs(self, body, args, request):
        text = "Unused items can be returned within 30 days of delivery for a full refund."
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"content": [{"type": "text", "text": text}]},
            },
        )
