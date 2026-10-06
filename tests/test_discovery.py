"""The products-first rule: a shopping request's first round is pinned to search_products,
after the reference rules, so terms and order questions still go to their own tools."""

from __future__ import annotations

import pytest
from commerce_common.grounding import first_forced_tool
from shopping_agent import ShoppingSessionState
from shopping_agent_runtime import orchestrator

from my_store import app as host  # installs the rule
from my_store.discovery import shopping_request


@pytest.mark.parametrize(
    "text",
    [
        "I need a gift",
        "help me plan a trip",
        "Compare two tours",
        "Mt Fuji vs Kuala Lumpur",
        "what phone plan should I get?",
        "do you sell eSIMs?",
        "something for a rainy day in Tokyo",
        "I'm going to Japan",
        "any ideas for my mum?",
        "what's good for a first-time visitor to Singapore?",
    ],
)
def test_shopping_requests_search_first(text):
    tool = first_forced_tool(orchestrator.GROUNDING_RULES, host.agent.config, text, ShoppingSessionState())
    assert tool == "search_products"


@pytest.mark.parametrize(
    "text",
    [
        "add the tee to my cart",
        "thanks, that's all",
        "yes please",
        "remember I'm vegetarian",
        "remove the SIM",
        "[App events since your last reply: Customer tapped Add to cart on Airport Transfer.]",
    ],
)
def test_other_messages_are_left_alone(text):
    assert not shopping_request(text)


def test_terms_questions_still_read_the_policies_first():
    text = "how many days do I have to return an item?"
    tool = first_forced_tool(orchestrator.GROUNDING_RULES, host.agent.config, text, ShoppingSessionState())
    assert tool == "search_policies"
