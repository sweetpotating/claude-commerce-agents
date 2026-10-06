# This assistant vs Shopify Inbox's AI agent

The goal is the most conversions from chat. Below, Shopify Inbox's features come from
Shopify's own help pages (help.shopify.com/en/manual/inbox, checked October 2026). This
assistant's features are the ones the evals and tests in this repository check.

## Where this assistant does more toward a sale

| A shopper wants to... | Shopify Inbox (Inbox agent) | This assistant |
|---|---|---|
| Buy without leaving the chat | Answers questions and recommends products; staff can send product links and discount codes. Building a cart in the chat is not described; the merchant sees the shopper's cart. | Adds items to a real Shopify cart from the chat or from card buttons, picks the variant (size), changes quantities, removes lines, and opens a pre-filled Shopify checkout (`cart-*`, `checkout-*` cases; persona cases reach checkout end to end). |
| Get products on the first reply | Conversation starters, then answers. | Shows products on the first reply to a vague request: the first round is pinned to a catalog search in code (`discovery-*`, 100% first-reply products). |
| Choose between options | Product answers and recommendations. | Side-by-side comparisons and plan tables built from catalog data (`compare-*`). |
| Plan something (a trip, a weekend, a gift set) | Not described. | Day-by-day itineraries and plans with the store's products on each step (`plan-001`). |
| Ask about the product they are looking at | Not described in the help pages. | Opening the chat on a product page shows that product with its own Add to cart; "does this come in M?" and "add this in S" work without naming it (`page-*`). |
| Keep shopping after a dead end | Hand-off to staff during availability hours. | Every reply ends with chips that lead to products; a tapped chip that finds nothing new still shows the closest products (`ends_with_chips` on every case). |
| Reach a person | Staff conversations, availability hours, order tracking. | Gives the store's contact email on any request for a person or a problem it can't solve (`handoff-*`). |

## Where Shopify Inbox does more today

- **Order tracking** with an order number and email. This assistant has no order
  lookup: Shopify's agent tools here don't return orders it didn't place. Add Customer
  Accounts MCP after shopper sign-in to close this gap.
- **Staff live chat** with merchant replies, AI-suggested replies for staff, and
  conversation labels. This assistant hands off by email; it has no live staff inbox.
- **Personalisation from a Shop session** (purchase history) and cited web reviews. This
  assistant remembers preferences only within a chat, and answers only from the store's
  own data, which keeps it from repeating unverified claims.
- **Discount codes.** Staff can send codes in Inbox. This assistant never invents a code
  (`guard-003`), and it has no discount tool.

## How we know, and how it keeps improving

- **Offline evals** (`evals/`, run with `scripts/loop.sh`). The baseline is 49 cases on the
  live store. Simulated shoppers reach checkout with the item they came for 5 of 5 times
  (median 4 turns). Every vague opener shows products in the first reply. Critical cases
  pass 100% and all cases 96%; the two failures have been fixed since. Replies take a
  median of 8 seconds (p90 14 seconds). A full run costs about $1. Harder cases (an
  unavailable size, a multi-item basket, a cheapest-option shopper, a hesitant shopper on
  a product page, cross-sell) are the next bar.
- **Live funnel** (`GET /api/metrics`): chats → products shown → added to cart →
  checkout shown → checkout clicked. Every event is logged, so a drop at one step shows
  where to look next.
- **The loop:** a failure, from an eval or a live funnel drop, becomes a case. Fix it,
  re-run the case 3 times, run the whole loop, then promote the baseline (`evals/README.md`).
  CI runs the offline checks on every push, and the live evals nightly once the
  repository has its secrets.

To compare directly, A/B the two on real traffic: show Inbox to half the visitors and
this assistant to the other half for two weeks. Compare checkout-click rate and orders
per chat, using the `utm_source` on this assistant's checkout links
(`CHECKOUT_UTM_SOURCE`).
