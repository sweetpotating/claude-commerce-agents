"""What the reference repo's four verticals add to the shopping agent, made to work on any
Shopify catalog, so one assistant serves products, trips and experiences, service plans,
and event tickets alike.

| Vertical (``vendor/commerce-agents/examples/``) | Brought over here |
| --- | --- |
| retail | the base agent: search, plans, comparisons, cart, checkout, policies |
| travel | ``present_itinerary`` (day-by-day card), date and party-size search notes |
| telecom | ``present_plan_comparison`` (side-by-side table), plan and fee policy terms |
| entertainment | ticket, fee, transfer and resale policy terms; per-item quantity cap |

Left out, because a Shopify store has no data behind them: the travel demo's room
availability and night-count booking, the telecom demo's subscriber account (current plan,
usage, upgrade eligibility), and the ticketing demo's seat map and seat holds. Those need a
backend that knows rooms, lines, or seats.
"""

from __future__ import annotations

import os
from typing import Any

from commerce_common.presentation import EnrichmentContext, PresentationExtension
from pydantic import BaseModel, Field
from shopping_agent import ShoppingAgentConfig, ShoppingSessionState

from .compare import COMPARE_NOTES

_DEFAULTS = ShoppingAgentConfig()

# Policy vocabulary from the travel, telecom, and ticketing configs: a question using any
# of these reads the store's policies before the agent answers.
POLICY_TERMS: tuple[str, ...] = (
    # travel and experiences
    "booking",
    "reschedule",
    "rescheduled",
    "no-show",
    "deposit",
    "free cancellation",
    "change fee",
    "weather",
    # service plans (telecom)
    "overage",
    "roaming",
    "data cap",
    "early termination",
    "activation fee",
    "plan change",
    "upgrade fee",
    "trade-in",
    "trade in",
    "autopay",
    "installment",
    "price guarantee",
    # tickets and events
    "service fee",
    "processing fee",
    "all-in",
    "face value",
    "waitlist",
    # Not a bare "transfer": stores sell airport transfers, and a product title in a
    # question or an app-event note would force a policy read on a shopping turn.
    "transferable",
    "transfer my",
    "transfer a ticket",
    "transfer the ticket",
    "transfer tickets",
    "transfer a booking",
    "transfer the booking",
    "ticket transfer",
    "resale",
    "sold out",
    "postponed",
    "will-call",
    "accessible seating",
)

SEARCH_NOTES = (
    "The catalog can hold services, experiences, trips, tours, classes, plans, tickets, "
    "and digital vouchers as well as physical goods; search before saying the store does "
    "not carry something. Dates, times, sessions, party sizes, and plan tiers usually "
    "appear as a product's options: when the customer names a date or a group size, match "
    "it to an option value and say plainly when no option fits. Prices are what the "
    "catalog says; state fees and what is included only from tool results."
)

# Every recommendation should end on something the shopper can open and buy.
DISCOVERY_NOTES = (
    "Show products in your first reply to any shopping request, however vague: do not ask "
    "a question before showing options. On this store the one-clarifying-question allowance "
    "does not cover a first reply: a missing recipient, budget, destination, or date is never "
    "a reason to reply with only a question. Assume the likeliest details, say the assumption "
    "in one short line, and put the alternatives in the chips as one-tap refinements. For "
    "example, 'I need a gift' searches 'gift' and shows picks across a few price points; "
    "'help me plan a trip' with no destination searches 'tour' and 'pass', shows two or "
    "three destinations the catalog covers, and lets the chips pick one. "
    "Recommendations lead to products. Before you present a plan, itinerary, guide, or "
    "comparison, search the catalog for each step or need it covers and attach the "
    "matching product_ids; run several searches when steps differ. When a step is "
    "something the store does not sell (flights, hotels, meals), keep it to one line "
    "and spend the card on what the store does sell. After a text-only answer or a "
    "guide, show the products it points to with present_products when any match. Of "
    "the turn's suggestion chips, make at least one a concrete next product search "
    "('Show more Tokyo day tours', 'Compare eSIM data plans'). After a cart add, make one "
    "chip a complementary item from a different category that serves the same goal (after "
    "a Japan eSIM: a Tokyo tour, travel insurance, a travel adapter; after a gift: a card or "
    "an add-on), named specifically and taken from results you have or a search you run in "
    "that turn, never added unasked. End every reply with 2-4 "
    "chips, and make every chip a step toward products: a specific search, a comparison "
    "that names its items, a plan, or adding a shown item; never a chip like 'Tell me "
    "more', 'Anything else?', or 'Something else'. Offer chips only for "
    "products and categories this session's results show the store carries (no chip for a "
    "destination, country, or category no result showed). A 'Show more X' chip needs a "
    "search that returned more X than you showed; a category chip ('spa gifts', 'outdoor "
    "gear') needs a result in that category. When a search, a tapped chip included, finds "
    "nothing new, say so in one line and still show the closest products you have with "
    "present_products, never a text-only reply. Write any budget or price in a "
    "chip in the catalog's currency, as the results show it. Product "
    "cards have their own Choose options, Add to cart, and Checkout buttons, so the "
    "shopper can buy straight from a card; you do not need to ask before they tap."
)

# Rules from the search and cart evals; each is also enforced or backed in code where it can
# be (shopify_backend: synonyms, catalog-wide price ranking; discovery: the cart is read
# before an edit; executor: an add that fits two products asks; chips.py: chips checked).
CATALOG_NOTES = (
    "Never say the store does not carry, sell, or have something unless a search in this "
    "turn found nothing for it; search the shopper's word and the catalog's own words for it "
    "first (a hotel or stay is a hotel voucher or villa stay; a SIM is an eSIM). For 'what do "
    "you sell' or 'what do you have', answer from the session context's "
    "current_page.extra.store_sells (the store's collections, with product counts and "
    "examples) without searching: name every collection, then show a few of the examples "
    "with present_products. For 'most expensive', 'priciest', 'cheapest', or any price "
    "ranking, search with those words in the query or sort by price: that ranks the whole "
    "catalog, while an ordinary search is a relevance cut that misses items. To change a "
    "quantity or remove an item, read the cart (get_cart) and then call update_cart_item or "
    "remove_from_cart; never answer a cart change from memory. When the shopper's words fit "
    "more than one product (two notebooks, two Japan eSIMs) and nothing said picks one, show "
    "them and ask which before adding. Chips offer only what this chat can do: search, "
    "compare, add, remove, change a quantity, check out; never 'notify me', 'back in stock "
    "alert', 'wishlist', or 'track price'. A chip that names a product names one a tool "
    "returned in this session."
)

# Where a shopper reaches a person. The store's contact FAQ gives the same address.
CONTACT_EMAIL = os.environ.get("STORE_CONTACT_EMAIL", "tanyueting96@gmail.com")

HANDOFF_NOTES = (
    "A person at the store is always reachable: when the customer asks for a human, a person, "
    "support, or a manager, wants to complain, or needs something you cannot do here (an "
    "order already placed, a refund, a change to a booking, a problem with a delivery), say "
    f"a person at the store can help and give the email {CONTACT_EMAIL}; read the store's "
    "contact FAQ with search_policies for how soon they reply. Never say there is no way to "
    "reach a person, and do not show products in that reply unless they ask."
)

BRAND_VOICE = (
    "warm, candid, and practical: plain about trade-offs, upfront about fees and what is "
    "left, and never in a hurry to sell"
)


def industry_config_overrides() -> dict[str, Any]:
    """Settings layered over the Shopify config (``shopify_agent_config``)."""
    return {
        "brand_voice": BRAND_VOICE,
        "domain_search_notes": " ".join(
            (
                os.environ.get("STORE_SEARCH_NOTES", SEARCH_NOTES),
                DISCOVERY_NOTES,
                CATALOG_NOTES,
                COMPARE_NOTES,
                HANDOFF_NOTES,
            )
        ),
        # Plan comparisons need every tier in one search (the telecom demo's setting).
        "max_search_results": 25,
        # The ticketing demo's cap: no one buys 40 of a ticket or a tour seat by mistake.
        "max_quantity_per_item": 8,
        "policy_intent_terms": _DEFAULTS.policy_intent_terms
        + tuple(t for t in POLICY_TERMS if t not in _DEFAULTS.policy_intent_terms),
    }


def _seen(state: ShoppingSessionState, ids: list[str]) -> list[dict[str, Any]]:
    return [
        state.seen_products[pid].model_dump(exclude_none=True)
        for pid in ids
        if isinstance(pid, str) and pid in state.seen_products
    ]


# -- present_itinerary (travel) --------------------------------------------------------


class ItineraryDay(BaseModel):
    label: str = Field(max_length=80)
    note: str | None = Field(default=None, max_length=280)
    product_ids: list[str] = Field(default_factory=list, max_length=6)


class ItineraryPayload(BaseModel):
    title: str = Field(max_length=80)
    days: list[ItineraryDay] = Field(min_length=1, max_length=10)
    travel_dates: str | None = Field(default=None, max_length=60)


_ITINERARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": 80},
        "days": {
            "type": "array",
            "minItems": 1,
            "maxItems": 10,
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "maxLength": 80, "description": "e.g. 'Day 1: Arrive'"},
                    "note": {"type": "string", "maxLength": 280},
                    "product_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                },
                "required": ["label"],
                "additionalProperties": False,
            },
        },
        "travel_dates": {"type": "string", "maxLength": 60},
    },
    "required": ["title", "days"],
    "additionalProperties": False,
}


def _itinerary_days(days: list[dict[str, Any]], state: ShoppingSessionState) -> list[dict[str, Any]]:
    out = []
    for day in days:
        if not isinstance(day, dict) or not day.get("label"):
            continue
        entry: dict[str, Any] = {
            "label": day["label"],
            "products": _seen(state, day.get("product_ids") or []),
        }
        if day.get("note"):
            entry["note"] = day["note"]
        out.append(entry)
    return out


async def _enrich_itinerary(payload: ItineraryPayload, context: EnrichmentContext) -> dict[str, Any]:
    enriched = payload.model_dump(exclude_none=True)
    enriched["days"] = _itinerary_days(enriched["days"], context.state)
    return enriched


def _partial_itinerary(data: dict[str, Any], state: ShoppingSessionState) -> dict[str, Any] | None:
    days = _itinerary_days(data.get("days") or [], state)
    if not days:
        return None
    payload: dict[str, Any] = {"title": data.get("title") or "", "days": days}
    if data.get("travel_dates"):
        payload["travel_dates"] = data["travel_dates"]
    return payload


def build_itinerary_extension() -> PresentationExtension:
    return PresentationExtension(
        name="present_itinerary",
        component="itinerary",
        description=(
            "Show a day-by-day itinerary for a trip, outing, or event weekend, with the "
            "store's tours, experiences, tickets, and gear attached to each day (e.g. "
            "'Day 1: Arrive'). Use when the customer is planning something that spans "
            "days; pass product_ids from this session's results and the UI fills in "
            "titles and prices. Know-how with no products attached goes in present_guide."
        ),
        input_schema=_ITINERARY_SCHEMA,
        payload_model=ItineraryPayload,
        enrich=_enrich_itinerary,
        enrich_partial=_partial_itinerary,
    )


# -- present_plan_comparison (telecom) --------------------------------------------------


class PlanAnnotation(BaseModel):
    plan_id: str
    best_for: str | None = Field(default=None, max_length=80)


class PlanMatrixPayload(BaseModel):
    title: str | None = Field(default=None, max_length=80)
    plan_ids: list[str] = Field(min_length=2, max_length=4)
    annotations: list[PlanAnnotation] = Field(default_factory=list, max_length=4)
    recommended_plan_id: str | None = None


_MATRIX_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": 80},
        "plan_ids": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4},
        "annotations": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "plan_id": {"type": "string"},
                    "best_for": {"type": "string", "maxLength": 80},
                },
                "required": ["plan_id"],
                "additionalProperties": False,
            },
        },
        "recommended_plan_id": {"type": "string"},
    },
    "required": ["plan_ids"],
    "additionalProperties": False,
}


# Attributes the UI uses itself rather than showing as a table row.
_HIDDEN_ATTRIBUTES = {"product_url", "image_urls"}


def _cell(value: Any) -> str:
    if value is None or value == "" or value == []:
        return "—"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


async def _enrich_matrix(payload: PlanMatrixPayload, context: EnrichmentContext) -> dict[str, Any]:
    plans = [context.state.seen_products[p] for p in payload.plan_ids if p in context.state.seen_products]
    if len(plans) < 2:
        raise ValueError(
            "A plan comparison needs at least 2 product_ids from this session's results. "
            "Search first and pick from the results."
        )
    # Every cell comes from the catalog record: price, seller, each option, any attribute.
    rows: list[dict[str, Any]] = [
        {"label": "Price", "values": [f"{p.price:g} {p.currency}" for p in plans]},
    ]
    if any(p.brand for p in plans):
        rows.append({"label": "By", "values": [_cell(p.brand) for p in plans]})
    for name in dict.fromkeys(k for p in plans for k in p.options):
        rows.append({"label": name, "values": [_cell(p.options.get(name)) for p in plans]})
    for key in dict.fromkeys(k for p in plans for k in p.attributes if k not in _HIDDEN_ATTRIBUTES):
        rows.append(
            {
                "label": key.replace("_", " ").capitalize(),
                "values": [_cell(p.attributes.get(key)) for p in plans],
            }
        )
    rows.append({"label": "Available", "values": ["Yes" if p.in_stock else "Sold out" for p in plans]})
    kept = {p.product_id for p in plans}
    enriched: dict[str, Any] = {
        "plans": [p.model_dump(exclude_none=True) for p in plans],
        "rows": rows,
        "annotations": [a.model_dump(exclude_none=True) for a in payload.annotations if a.plan_id in kept],
    }
    if payload.title:
        enriched["title"] = payload.title
    if payload.recommended_plan_id in kept:
        enriched["recommended_plan_id"] = payload.recommended_plan_id
    return enriched


def build_plan_matrix_extension() -> PresentationExtension:
    return PresentationExtension(
        name="present_plan_comparison",
        component="plan_matrix",
        description=(
            "Show a side-by-side table of 2-4 plans, packages, passes, tiers, or ticket "
            "types: price, options, and availability in rows. Use for 'which plan / package "
            "should I pick' decisions; for physical products use present_comparison. Pass "
            "product_ids from this session's results; the UI fills every cell from the "
            "catalog. You may add a short 'best for' note per plan and recommend one."
        ),
        input_schema=_MATRIX_SCHEMA,
        payload_model=PlanMatrixPayload,
        enrich=_enrich_matrix,
    )


def industry_extensions() -> list[PresentationExtension]:
    return [build_itinerary_extension(), build_plan_matrix_extension()]
