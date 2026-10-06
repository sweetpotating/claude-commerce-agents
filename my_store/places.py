"""Countries and the cities and landmarks travel products are named after.

Shopify's catalog search matches words in product titles, and travel products are titled
by city or landmark: "Mt Fuji Day Trip from Tokyo", "Kuala Lumpur City Tour". Live, "Japan
or Malaysia tours" therefore found only the Japan eSIM and the Malaysia SIM, and the agent
told the shopper the store had no Japan or Malaysia trips. search_products expands a
country in the query to these names (shopify_backend.search_products).
"""

from __future__ import annotations

import re

PLACES: dict[str, tuple[str, ...]] = {
    "japan": ("tokyo", "osaka", "kyoto", "fuji", "hokkaido"),
    "malaysia": ("kuala lumpur", "penang", "langkawi"),
    "thailand": ("bangkok", "phuket", "chiang mai"),
    "vietnam": ("hanoi", "ho chi minh", "da nang"),
    "korea": ("seoul", "busan", "jeju"),
    "taiwan": ("taipei",),
    "indonesia": ("bali", "jakarta"),
    "singapore": ("sentosa", "changi"),
    "hong kong": ("victoria harbour", "disneyland hong kong"),
    "china": ("shanghai", "beijing"),
    "philippines": ("manila", "cebu", "boracay"),
    "australia": ("sydney", "melbourne"),
    "usa": ("new york", "los angeles", "las vegas"),
    "united states": ("new york", "los angeles"),
    "uk": ("london",),
    "united kingdom": ("london",),
    "france": ("paris",),
    "italy": ("rome", "venice"),
}
PLACE_NAMES = tuple(sorted({p for ps in PLACES.values() for p in ps}, key=len, reverse=True))
ALIASES = {"south korea": "korea", "america": "usa", "us": "usa", "england": "uk", "britain": "uk"}


def expansions(query: str, limit: int = 8) -> list[str]:
    """Extra queries for a query naming a country: each place alone (titles rarely also hold
    the product word), and the query with the country swapped for the place."""
    text = query.lower()
    alone: list[str] = []
    swapped_all: list[str] = []
    for name in sorted({**PLACES, **dict.fromkeys(ALIASES)}, key=len, reverse=True):
        if not re.search(rf"\b{re.escape(name)}\b", text):
            continue
        country = ALIASES.get(name, name)
        for place in PLACES.get(country, ()):
            alone.append(place)
            swapped = re.sub(rf"\b{re.escape(name)}\b", place, text).strip()
            if swapped != place:
                swapped_all.append(swapped)
        text = re.sub(rf"\b{re.escape(name)}\b", " ", text)  # don't expand "korea" inside "south korea" twice
    # A place alone matches titles best ("Mt Fuji Day Trip from Tokyo" has no "tour").
    out = [q for q in dict.fromkeys(alone + swapped_all) if q != query.lower()]
    return out[:limit]
