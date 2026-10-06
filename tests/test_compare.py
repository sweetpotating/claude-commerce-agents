"""Comparisons in every vertical: facts read from the store's own text become comparable
rows, and a country in a search also finds products titled by its cities (live: "Japan or
Malaysia tours" found only SIMs, and the agent said the store had no such trips)."""

from __future__ import annotations

from datetime import datetime

import httpx
from shopping_agent import ShoppingSessionContext

from my_store.facts import extract
from my_store.places import expansions
from my_store.shopify_backend import ShopifyUCPBackend

from .fake_shopify import SHOP, FakeShopifyStore

TERMS = (
    "Hand-picked for quality and value. Product details Instant e-voucher delivered by email "
    "after purchase Flexible dates: free changes up to 24 hours before "
    "Free cancellation up to 48 hours before start"
)


def test_facts_per_vertical_from_live_titles_and_descriptions():
    # Telecom / connectivity: coverage, type, data, validity, value per day.
    esim = extract("Japan eSIM - 7 Days 10GB", "Prepaid data eSIM, QR code by email.", 19.9, "SGD", False)
    assert esim["Coverage"] == "Japan" and esim["Data"] == "10 GB" and esim["Valid for"] == "7 days"
    assert esim["Per day"] == "2.84 SGD" and esim["Delivery"] == "Digital, by email"
    sim = extract("Malaysia Prepaid SIM - 7 Days", "Physical SIM, 20GB data.", 9.9, "SGD", True)
    assert sim["Type"] == "Physical SIM" and sim["Data"] == "20 GB" and sim["Delivery"] == "Shipped"
    assert (
        extract("Postpaid Plan Plus 100GB (demo)", "Monthly plan listing, demo only.")["Billing"] == "Monthly"
    )
    # Travel: stays and tours.
    hotel = extract(
        "Bangkok Hotel Voucher - 3 Nights", "3 nights in a partner 4-star hotel. " + TERMS, 360, "SGD"
    )
    assert hotel["Nights"] == "3" and hotel["Hotel"] == "4-star" and hotel["Per night"] == "120.00 SGD"
    assert hotel["Free cancellation"] == "up to 48 h before"
    tour = extract("Mt Fuji Day Trip from Tokyo", "Guided coach tour with lunch. " + TERMS, 110, "SGD")
    assert tour["Duration"] == "Full day" and tour["Includes"] == "lunch"
    # Ticketing and experiences.
    assert extract("Cinema Tickets Premium (2)", "Premium seats, e-tickets.")["Pack"] == "2 tickets"
    assert extract("Karaoke Room 3 Hours", "Private room, up to 8 people.")["Group size"] == "up to 8 people"
    # Retail: books and games.
    assert (
        extract("Atomic Habits", "Small changes. By James Clear. Pick of the shelf")["Author"]
        == "James Clear"
    )
    game = extract("Strategy Board Game: Ticket to Ride", "For 2 to 5 players, ages 8 and up.")
    assert game["Players"] == "2-5" and game["Ages"] == "8+"


def test_facts_never_guess():
    # Goods the store's template calls "e-voucher" with cancellation terms: Shopify's shipping
    # flag decides delivery, and booking terms stay off goods.
    scale = extract("Luggage Scale", "Digital hanging scale to 50kg. " + TERMS, 12, "SGD", True)
    assert scale == {"Delivery": "Shipped"}
    assert "Per day" not in extract("Cinema Tickets (2 Standard)", "Valid 60 days, e-tickets.", 22, "SGD")
    assert extract("Bluetooth Speaker", "Portable speaker.") == {}


def test_a_country_expands_to_the_places_products_are_named_after():
    assert expansions("Japan")[:4] == ["tokyo", "osaka", "kyoto", "fuji"]
    both = expansions("trips for Japan or Malaysia")
    assert "kuala lumpur" in both and "tokyo" in both
    assert expansions("south korea")[0] == "seoul"
    assert expansions("tent") == []


async def test_a_country_search_finds_city_titled_tours():
    store = FakeShopifyStore()
    backend = ShopifyUCPBackend(SHOP, buyer_country="US", http=httpx.AsyncClient(transport=store.transport()))
    session = ShoppingSessionContext(session_id="s1", user_id="g", now=datetime(2026, 10, 6))
    found = await backend.search_products(session, "Malaysia")
    assert [p.title for p in found] == ["Kuala Lumpur City Tour"]
    assert found[0].attributes["Duration"] == "Half day" and found[0].attributes["Includes"] == "lunch"
