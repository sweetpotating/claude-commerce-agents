"""Comparable facts read from a product's title and description, so a comparison has rows
to compare in every vertical: data and validity for eSIMs and plans, nights and stars for
hotel vouchers, duration and inclusions for tours, group size for experiences, players for
games, authors for books, plus delivery, cancellation, and a price per day or night.

Shopify gives these products no structured specs (no metafields on this path), so a side-
by-side table had only price and availability, and the model compared from memory. The
facts become product attributes (shopify_backend._product): the model reads them in every
search result, present_plan_comparison turns each into a row, and chat.html draws them
under every comparison. Only text the store wrote is used; a fact the text does not state
is left out, never guessed.
"""

from __future__ import annotations

import re

_INCLUDES = (
    "lunch",
    "breakfast",
    "dinner",
    "hotel pickup",
    "snorkel gear",
    "welcome drink",
    "shoe rental",
    "transit credit",
    "attractions",
    "skyline views",
)


_BOOKABLE = re.compile(
    r"\b(tour|trip|ticket|pass|voucher|stay|cruise|transfer|insurance|admission|class|room|show)\b",
    re.IGNORECASE,
)
# Products whose length is what you pay for, so a price per day compares them.
_PER_DAY = re.compile(r"\b(e?sim|data|pass|insurance|plan)\b", re.IGNORECASE)


def _first(pattern: str, *texts: str, flags: int = re.IGNORECASE) -> re.Match | None:
    for text in texts:
        if text and (m := re.search(pattern, text, flags)):
            return m
    return None


def _plural(n: str, word: str) -> str:
    return f"{n} {word}" if n == "1" else f"{n} {word}s"


def extract(
    title: str,
    description: str | None,
    price: float | None = None,
    currency: str = "",
    ships: bool | None = None,
) -> dict[str, str]:
    """Facts as {label: value}, in reading order. Labels are what a table row shows."""
    t, d = title or "", (description or "").split("Hand-picked for quality")[0]
    tail = description or ""
    facts: dict[str, str] = {}

    # Connectivity: coverage, type, data, validity.
    sim = re.search(r"^(.*?)\s+(e?SIM|Prepaid SIM)\b", t, re.IGNORECASE)
    if sim or re.search(r"\b(esim|sim|data pass|plan)\b", t, re.IGNORECASE):
        if sim and sim.group(1):
            facts["Coverage"] = sim.group(1).strip()
        if re.search(r"\besim\b", t, re.IGNORECASE):
            facts["Type"] = "eSIM (QR code by email)"
        elif re.search(r"physical sim|prepaid sim", t + " " + d, re.IGNORECASE):
            facts["Type"] = "Physical SIM"
    if m := _first(r"(\d+(?:\.\d+)?)\s?GB\b", t, d):
        facts["Data"] = f"{m.group(1)} GB"
    calling = re.search(r"\bcalling\b", t, re.IGNORECASE)
    if m := _first(r"\b(\d+)\s?mins?\b", t, d) or (calling and _first(r"\b(\d+)[\s-]?minutes?\b", t, d)):
        facts["Calls"] = f"{m.group(1)} minutes"
    days = None
    if m := _first(r"\b(\d+)[\s-]?Days?\b", t, d):
        days = m.group(1)
        facts["Valid for"] = _plural(days, "day")
    elif re.search(r"\bmonthly\b|/month\b", d, re.IGNORECASE):
        facts["Billing"] = "Monthly"
    if m := _first(r"\b(\d+)\s?Months?\b", t):
        facts["Valid for"] = _plural(m.group(1), "month")

    # Stays.
    nights = None
    if m := _first(r"\b(\d+)\s?Nights?\b", t, d):
        nights = m.group(1)
        facts["Nights"] = nights
    if m := _first(r"\b(\d)-star\b", d):
        facts["Hotel"] = f"{m.group(1)}-star"

    # Experiences: duration, inclusions, group size.
    if m := _first(r"\b(\d+)[\s-]?(?:hours?|hrs?)\b", t, d):
        facts["Duration"] = _plural(m.group(1), "hour")
    elif not calling and (m := _first(r"\b(\d+)[\s-]?minutes?\b", t, d)):
        facts["Duration"] = f"{m.group(1)} minutes"
    elif re.search(r"half[\s-]day", t + " " + d, re.IGNORECASE):
        facts["Duration"] = "Half day"
    elif re.search(r"full[\s-]day|day trip", t + " " + d, re.IGNORECASE):
        facts["Duration"] = "Full day"
    elif re.search(r"\b(evening|night)\b", t + " " + d, re.IGNORECASE) and re.search(
        r"tour|cruise|market|show", t, re.IGNORECASE
    ):
        facts["Duration"] = "Evening"
    included = [i for i in _INCLUDES if re.search(rf"\b{i}\b", d, re.IGNORECASE)]
    if included:
        facts["Includes"] = ", ".join(included)
    if m := _first(r"\bfor (\d+)\b", t) or _first(r"up to (\d+) people", d):
        facts["Group size"] = (
            f"up to {m.group(1)} people" if "up to" in m.group(0) else f"{m.group(1)} people"
        )
    if m := _first(r"\b(\d+) to (\d+) players\b", d):
        facts["Players"] = f"{m.group(1)}-{m.group(2)}"
    if m := _first(r"\bages? (\d+)(?: and up|\+)", d):
        facts["Ages"] = f"{m.group(1)}+"
    if m := re.search(r"\((\d+)(?:\s+\w+)?\)", t):
        noun = "tickets" if re.search(r"ticket", t, re.IGNORECASE) else "pieces"
        facts["Pack"] = f"{m.group(1)} {noun}"
    if m := re.search(r"\bBy ([A-Z][\w.'-]+(?: (?:&|and) [A-Z][\w.'-]+| [A-Z][\w.'-]+){0,3})\.", d):
        facts["Author"] = m.group(1)

    # Terms the store states.
    # Shopify's own shipping flag decides, when given: the description template calls a
    # luggage scale an "e-voucher".
    if ships is True:
        facts["Delivery"] = "Shipped"
    elif ships is False or re.search(r"e-voucher|digital delivery|by email|gift code|e-tickets?", tail, re.I):
        facts["Delivery"] = "Digital, by email"
    elif re.search(r"\bships?\b|shipping", tail, re.IGNORECASE):
        facts["Delivery"] = "Shipped"
    # Cancellation and date terms belong to bookings; the store's description template also
    # puts them on goods (a luggage scale "free cancellation up to 48 h"), so read them only
    # for things that are booked.
    if _BOOKABLE.search(t):
        if m := _first(r"free cancellation up to (\d+) hours", tail):
            facts["Free cancellation"] = f"up to {m.group(1)} h before"
        if m := _first(r"free changes up to (\d+) hours", tail):
            facts["Date changes"] = f"free up to {m.group(1)} h before"

    # Value: price per day or per night, when the product states its length.
    if price:
        if nights and int(nights) > 1:
            facts["Per night"] = f"{price / int(nights):.2f} {currency}".strip()
        elif days and int(days) > 1 and (facts.get("Coverage") or _PER_DAY.search(t)):
            facts["Per day"] = f"{price / int(days):.2f} {currency}".strip()
    return facts
