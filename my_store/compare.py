"""present_comparison for this store: a table with a value in every cell, grounded ids only,
no empty card while it streams, and "these two" / "this" resolved for the model.

The reference tool (shopping_agent.enrichment) sends ``{product_id, pros, cons, best_for,
product}`` per entry and the model's ``dimensions`` as bare names, so a client drawing a
table got headers with nothing under them. It also drops an unknown id quietly when two
others remain, and its first streamed frames carry ``entries: []``. Here:

- Each entry carries ``values``: one cell per dimension. The store's own facts fill a cell
  first (price, availability, rating, options, the attributes facts.py reads from the
  title and description); the model's ``values`` fill only what the store does not state;
  a cell neither states is "—". ``dimensions`` is the final row order: Price, the model's
  dimensions, then every other fact any of the products states.
- Every product_id must come from this session's results; one that did not refuses the
  whole call, naming it, so pros and cons are never written about an unseen product.
- No streamed frame until two entries resolve to products.
- The host knows what "this" (the product page open) and "these" (the products the last
  reply showed) mean and puts both in the session context (``context_extra``).
- A shopper's comparison wording that the model answers with present_products becomes a
  comparison (``as_comparison``), and a comparison reply with no text gets a one-line
  summary from the table (``summary``).
"""

from __future__ import annotations

import re
from typing import Any

from commerce_common.presentation import EnrichmentContext, PresentationComponent, PresentationRefused
from pydantic import Field
from shopping_agent import Product, ShoppingSessionState
from shopping_agent.enrichment import PRESENTATION_COMPONENTS, comparison_price_delta
from shopping_agent.gates import PROVENANCE_GATE
from shopping_agent.tools.presentation import ComparisonEntry, PresentComparisonPayload

TOOL = "present_comparison"
HIDDEN_FACTS = {"product_url", "image_urls"}
MAX_ROWS = 10
EMPTY = "—"

# A dimension the model names, mapped to the fact the store states under another name.
_SYNONYMS = {
    "cost": "Price",
    "price": "Price",
    "prices": "Price",
    "value": "Price",
    "stock": "Availability",
    "availability": "Availability",
    "in stock": "Availability",
    "rating": "Rating",
    "reviews": "Rating",
    "length": "Duration",
    "time": "Duration",
    "duration": "Duration",
    "sizes": "Options",
    "size": "Options",
    "options": "Options",
    "cancellation": "Free cancellation",
    "refund": "Free cancellation",
    "shipping": "Delivery",
    "delivery": "Delivery",
}


class Entry(ComparisonEntry):
    values: dict[str, str] = Field(default_factory=dict)


class Payload(PresentComparisonPayload):
    entries: list[Entry] = Field(min_length=2, max_length=4)


DESCRIPTION = (
    "Compare 2-4 products side by side in a table: one row per dimension, one cell per "
    "product. Use it whenever the customer asks to compare, asks how products differ, which "
    "is better, or says 'X vs Y', for products just shown ('these two', 'them') or named. "
    "Every product_id must come from a search_products or get_product_details result in this "
    "session: search each named product first, one search per item; an id from nowhere "
    "refuses the call. The host fills each cell from the store's data (price, availability, "
    "rating, options, and the facts in each product's attributes); add `values` only for a "
    "dimension the attributes do not state, copied from a tool result. Always also write one "
    "or two sentences naming the difference that decides the choice."
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": 80, "description": "Heading for the comparison."},
        "entries": {
            "type": "array",
            "minItems": 2,
            "maxItems": 4,
            "description": "The products being compared, in this session's results.",
            "items": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "A product_id a tool returned in this session.",
                    },
                    "pros": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 4,
                        "description": "Short advantages, from tool results.",
                    },
                    "cons": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 3,
                        "description": "Short drawbacks, from tool results.",
                    },
                    "best_for": {
                        "type": "string",
                        "maxLength": 80,
                        "description": "Who or what this option suits best.",
                    },
                    "values": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": (
                            "Cells for dimensions the product's attributes do not state, "
                            "keyed by dimension name, each copied from a tool result."
                        ),
                    },
                },
                "required": ["product_id"],
                "additionalProperties": False,
            },
        },
        "dimensions": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 6,
            "description": "The dimensions the customer is weighing; the table shows these first.",
        },
        "recommended_product_id": {"type": "string", "description": "The entry you recommend."},
    },
    "required": ["entries"],
    "additionalProperties": False,
}

COMPARE_NOTES = (
    "Comparisons: present_comparison is the tool for any request to compare, 'X vs Y', "
    "'difference between X and Y', or 'which is better', never present_products. Run at "
    "once: a request that names nothing ('compare two tours') compares the two or three "
    "strongest candidates from this session's latest results without asking which. 'These', "
    "'these two', 'them', 'both' mean the products in the session context's "
    "current_page.extra.last_shown; 'this', 'this one', 'it' on a product page mean "
    "current_page.extra.viewing; never ask which products those are and never search for "
    "others in their place. A request that names products ('Mt Fuji vs Kuala Lumpur', "
    "'this with the mug') searches each named one not yet shown by its key word ('Fuji', "
    "'mug'), one search per item, then compares the best match for each; pass only ids from "
    "those results. When a named product is not in the catalog, say so in one line; offer "
    "instead only products of the same kind (same product word or category) from the "
    "results, and when there are none, say the store has nothing close to compare it with; "
    "never pad a comparison with unrelated products. The table shows each product's facts "
    "(price, data, validity, nights, duration, inclusions, group size, cancellation) side "
    "by side, so name the one or two differences that decide the choice in one or two "
    "sentences of text with the card, never a card alone, and never compare on a fact no "
    "product states. Products from different countries or categories can be compared when "
    "the shopper asks. A comparison chip names its items ('Compare Mt Fuji and Phuket "
    "tours'), never just 'Compare two tours'."
)


def money(price: float, currency: str) -> str:
    return f"{currency} {price:,.2f}"


def facts_of(product: Product) -> dict[str, str]:
    """Every fact the store states for a product, in display order."""
    facts = {"Price": ("from " if product.options else "") + money(product.price, product.currency)}
    for key, value in product.attributes.items():
        if key not in HIDDEN_FACTS and value not in (None, ""):
            facts[key] = str(value)
    for name, values in product.options.items():
        facts[name] = ", ".join(values)
    if product.rating is not None:
        reviews = f" ({product.review_count})" if product.review_count else ""
        facts["Rating"] = f"{product.rating:g}/5{reviews}"
    facts["Availability"] = "In stock" if product.in_stock else "Sold out"
    return facts


def _canonical(name: str, known: list[str]) -> str:
    """The store's name for a dimension the model wrote ('cost' -> 'Price')."""
    low = name.strip().lower()
    for key in known:
        if key.lower() == low:
            return key
    return _SYNONYMS.get(low, name.strip())


def table(
    products: list[Product], dimensions: list[str], model_values: list[dict[str, str]]
) -> tuple[list[str], list[dict[str, str]]]:
    """Rows and a full set of cells per product: store facts, then the model's values for
    what the store does not state, then a dash. A row nobody states is left out."""
    facts = [facts_of(p) for p in products]
    known = list(dict.fromkeys(k for f in facts for k in f))
    stated = [
        {_canonical(k, known): str(v) for k, v in (given or {}).items() if str(v).strip()}
        for given in model_values
    ]
    wanted = [_canonical(d, known) for d in dimensions]
    rows = list(dict.fromkeys(["Price", *wanted, *known]))
    cells = [
        {row: f.get(row) or s.get(row) or EMPTY for row in rows} for f, s in zip(facts, stated, strict=True)
    ]
    rows = [row for row in rows if any(c[row] != EMPTY for c in cells)][:MAX_ROWS]
    # Availability says nothing when every product is in stock.
    if "Availability" in rows and all(c["Availability"] == "In stock" for c in cells):
        rows.remove("Availability")
    return rows, [{row: c[row] for row in rows} for c in cells]


def _record(product: Product) -> dict[str, Any]:
    return product.model_dump(exclude_none=True)


async def enrich(payload: Payload, context: EnrichmentContext) -> dict[str, Any]:
    entries = list({e.product_id: e for e in payload.entries}.values())  # one per product
    unknown = [e.product_id for e in entries if e.product_id not in context.state.seen_products]
    if unknown:
        raise PresentationRefused(
            f"Not shown: {', '.join(unknown)} did not come from this session's results. "
            "Search for each product you compare (one search per item) and call "
            "present_comparison again with the product_ids those results return.",
            PROVENANCE_GATE,
        )
    page = _turn_this.get(id(context.state))
    if (
        page
        and page[0] not in {e.product_id for e in entries}
        and not any(context.state.seen_products[e.product_id].variant_of == page[0] for e in entries)
    ):
        raise PresentationRefused(
            f"The shopper said 'this' on the page for {page[1]} ({page[0]}): compare that "
            "product, not one from earlier in the chat. Call present_comparison again with it.",
            PROVENANCE_GATE,
        )
    if len(entries) < 2:
        raise PresentationRefused("A comparison needs 2-4 different products.", PROVENANCE_GATE)
    products = [context.state.seen_products[e.product_id] for e in entries]
    rows, cells = table(products, payload.dimensions, [e.values for e in entries])
    enriched = payload.model_dump(exclude_none=True)
    enriched["dimensions"] = rows
    enriched["entries"] = [
        e.model_dump(exclude_none=True) | {"values": c, "product": _record(p)}
        for e, c, p in zip(entries, cells, products, strict=True)
    ]
    if (delta := comparison_price_delta(enriched["entries"])) is not None:
        enriched["price_delta"] = delta
    return enriched


def partial(data: dict[str, Any], state: ShoppingSessionState) -> dict[str, Any] | None:
    """A streaming frame only once two entries resolve to products: never an empty card."""
    found: list[tuple[dict[str, Any], Product]] = []
    for entry in data.get("entries") or []:
        if isinstance(entry, dict) and (product := state.seen_products.get(entry.get("product_id"))):
            if all(product.product_id != p.product_id for _, p in found):
                found.append((entry, product))
    if len(found) < 2:
        return None
    dims = [d for d in data.get("dimensions") or [] if isinstance(d, str)]
    values = [e.get("values") if isinstance(e.get("values"), dict) else {} for e, _ in found]
    rows, cells = table([p for _, p in found], dims, values)
    payload: dict[str, Any] = {
        "dimensions": rows,
        "entries": [
            {
                "product_id": p.product_id,
                "pros": e.get("pros") or [],
                "cons": e.get("cons") or [],
                "best_for": e.get("best_for"),
                "values": c,
                "product": _record(p),
            }
            for (e, p), c in zip(found, cells, strict=True)
        ],
    }
    for key in ("title", "recommended_product_id"):
        if data.get(key):
            payload[key] = data[key]
    return payload


SPEC = PresentationComponent(
    name=TOOL, component="comparison", payload_model=Payload, enrich=enrich, enrich_partial=partial
)


def install() -> None:
    """Swap the built-in component before the agent is built (the runtime and the executor
    read this dict); ``install_tool`` then swaps the tool definition the model sees."""
    PRESENTATION_COMPONENTS[TOOL] = SPEC


def install_tool(agent: Any) -> None:
    agent._tools = [
        t | {"description": DESCRIPTION, "input_schema": INPUT_SCHEMA} if t.get("name") == TOOL else t
        for t in agent._tools
    ]
    agent._specs[TOOL] = SPEC


# -- The shopper's words ---------------------------------------------------------------

COMPARE_INTENT = re.compile(
    r"\b(compare|comparison|vs\.?|versus|difference|differences|differ|which (one )?is better|"
    r"which (one )?should i (get|pick|choose|buy)|better[,:]? .+ or )\b",
    re.IGNORECASE,
)
# "compare these two", "which is better of them": names nothing new, so no search is needed.
_REFERENCE = re.compile(r"\b(these|those|them|both|the two|the 2|the three|either)\b", re.IGNORECASE)
_FILLER = set(
    "compare comparison these those them both the two 2 three 3 ones one items products of "
    "which is better what's whats what difference differences between how do does they differ "
    "vs versus and please can you me for a an tell show which should i get pick "
    "choose buy".split()
)

_turn_wants_comparison: dict[int, bool] = {}
# The product page the shopper is on, per chat, when their words say "this" (item 16).
_turn_this: dict[int, tuple[str, str]] = {}
_THIS = re.compile(r"\b(this|this one|it)\b", re.IGNORECASE)


def note_viewing(state: ShoppingSessionState, text: str, viewing: Product | None) -> None:
    if viewing is not None and _THIS.search(text):
        _turn_this[id(state)] = (viewing.product_id, viewing.title)
    else:
        _turn_this.pop(id(state), None)


def wants_comparison(text: str) -> bool:
    return bool(COMPARE_INTENT.search(text))


def refers_to_shown(text: str) -> bool:
    """A comparison of products already shown, naming nothing new ('compare these two')."""
    if not (wants_comparison(text) and _REFERENCE.search(text)):
        return False
    words = re.findall(r"[a-z0-9']+", text.lower())
    return all(w in _FILLER for w in words)


def note_turn(state: ShoppingSessionState, text: str) -> None:
    _turn_wants_comparison[id(state)] = wants_comparison(text)


def as_comparison(state: ShoppingSessionState, tool_input: dict[str, Any] | None) -> dict[str, Any] | None:
    """present_products of 2-4 picks in a turn that asked to compare, as a comparison."""
    picks = (tool_input or {}).get("picks") or []
    if not _turn_wants_comparison.get(id(state)) or not 2 <= len(picks) <= 4:
        return None
    entries = []
    for pick in picks:
        if isinstance(pick, dict) and pick.get("product_id"):
            entry: dict[str, Any] = {"product_id": pick["product_id"]}
            if pick.get("reason"):
                entry["best_for"] = str(pick["reason"])[:80]
            entries.append(entry)
    if len(entries) < 2:
        return None
    out: dict[str, Any] = {"entries": entries}
    if (tool_input or {}).get("title"):
        out["title"] = tool_input["title"]
    return out


def context_extra(viewing: Product | None, last_shown: list[dict[str, Any]]) -> dict[str, Any]:
    """What 'this' and 'these' mean, for the session context's current_page.extra."""
    extra: dict[str, Any] = {}
    if viewing is not None:
        extra["viewing"] = {
            "product_id": viewing.product_id,
            "title": viewing.title,
            "price": money(viewing.price, viewing.currency),
        }
    if last_shown:
        extra["last_shown"] = last_shown
    return extra


def summary(payload: dict[str, Any]) -> str:
    """One line for a comparison reply that came with no text: the price gap and the facts
    that differ, from the table itself."""
    entries = [e for e in payload.get("entries") or [] if e.get("product")]
    if len(entries) < 2:
        return ""
    names = {e["product_id"]: e["product"]["title"] for e in entries}
    parts = []
    if d := payload.get("price_delta"):
        currency = entries[0]["product"].get("currency", "")
        parts.append(
            f"{names[d['low_product_id']]} is {money(d['amount'], currency)} less than "
            f"{names[d['high_product_id']]}."
        )
    differ = [
        row
        for row in payload.get("dimensions") or []
        if row not in ("Price", "Availability")
        and EMPTY not in (cells := [e.get("values", {}).get(row, EMPTY) for e in entries])
        and len(set(cells)) > 1
    ][:2]
    if differ:
        bits = [
            f"{row.lower()} ({' vs '.join(e['values'].get(row, EMPTY) for e in entries)})" for row in differ
        ]
        parts.append("They differ on " + " and ".join(bits) + ".")
    if not parts:
        parts.append("Here they are side by side.")
    return " ".join(parts)
