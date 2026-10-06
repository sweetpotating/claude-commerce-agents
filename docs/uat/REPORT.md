# UAT: shopping agent on the live iKnowledge Shopify store

Run 2026-10-06 against `iknowledge-dev.myshopify.com`, with `STORE_BACKEND=shopify`, the
reference agent at `fd4d592`, and `my_store/app.py` under uvicorn. Each chat used a fresh
session over `/api/session` and `/api/chat` (SSE). Unless a row says otherwise, the runs
used `SHOPIFY_BUYER_COUNTRY=SG` (see finding 1).

**The store:** 66 products. Books, merch, and a few physical gadgets ship. Tours, city
passes, hotel vouchers, eSIMs, demo postpaid and fibre plans, and event tickets are
non-shipping. Prices are in SGD. One product has options: the Logo Tee in sizes S, M and L.

## Results

| # | Scenario | Result | UI components emitted |
|---|---|---|---|
| 0 | `scripts.shopify_smoke --query tee` | **Pass**, 7 of 7 steps (with `SG`). With the environment's `US` it **failed** at add to cart. | — |
| a | Retail: tee, size M, add, "how many days to return?", checkout | **Pass** | `products`, `suggestions`, `checkout` (Shopify `/cart/c/…` handoff URL) |
| b | Travel: "Plan a 2-3 day trip to Singapore using what you sell" | **Pass** (3 runs) | `plan` in 2 runs, `itinerary` in 1. All used real `gid://shopify/Product/…` ids: Airport Transfer, SG Prepaid SIM, City Pass, Universal Studios. |
| c | Telecom: "Compare your mobile plans side by side" | **Pass** | `plan_matrix` (Plus 100GB vs Starter 30GB, real ids, home fibre left out as not mobile) |
| d | Ticketing: "Can I transfer or resell a booking, and are there any service fees?" | **Pass** after fix | `suggestions`. Answer comes from `search_policies`; no fees invented. |
| e1 | Guardrail: 20 Universal Studios tickets | **Pass** | Capped at 8; the agent said so ("only add 8… per-item limit"). |
| e2 | Guardrail: "Do you sell surfboards?" | **Pass** | Searched "surfboard" then "surf", said the store doesn't carry them, named no other retailer. |
| f | Discovery: "Plan a Tokyo trip" (then "Solo, first-time visitor, 4 days next month") | **Pass**, with notes below | Turn 1 asked a clarifying question; none of its chips was a product search. Turn 2: `plan` with the hotel voucher, Japan eSIM and Mt Fuji day trip, each linked to `/products/{handle}`. The one step the store can't cover ("City exploring") has no products. One chip is a product search ("Search for Tokyo food tours"). |
| g | One-tap buying (no chat turn between taps): plan → `/api/cart/add` ×2 → tee `/api/product/options` → add size S → `/api/checkout` → "anything else I need?" | **Pass** after a fix | The checkout card held all three items (Tee S, Airport Transfer, SG SIM, SGD 95.90). The handoff link redirects to `/checkouts/cn/…/en-sg`, the real Shopify checkout. The follow-up turn listed what was in the cart, re-added nothing, and pointed out the plan items not yet added. |
| g-UI | Headless Chromium: Choose options → size → Add to cart → checkout bar → `#checkout-go` | **Pass** | The bar read "Checkout · 1 item · SGD 29.90", and `#checkout-go` linked to the Shopify `/cart/c/…` checkout. Screenshot: [`happy-path.png`](happy-path.png) |
| h | Vague first messages, 2 fresh sessions each: "I need a gift", "help me plan a trip", "what phone plan should I get?", "what should I look for in a travel eSIM?" | **Pass** after a fix (8 of 8) | Before the fix, 2 of 4 failed: gift and trip replied with only a question. After it: gift shows `products` (5 real ids); trip shows `plan` (Tokyo, with Singapore, Bangkok, KL and Phuket as chips); phone plan shows `comparison`/`products` with both postpaid plans; eSIM shows `guide` + `products`. |
| UI | `chat.html` travel turn in headless Chromium | **Pass** | No `<pre>` and no raw JSON. Itinerary and plan cards render. Screenshot: [`travel-turn.png`](travel-turn.png) |
| R | Render deploy `/healthz` and one chat turn | **Not run** | This environment's network policy rejects `iknowledge-shopping-agent.onrender.com`. |

Checks after the fixes: `ruff check .`, `ruff format --check .`, and `python -m pytest -q` all
pass (19 tests).

### Round (i): regression of the core features (after the live "object can not be found" drop)

This round ran on `208c40f` (keep-alive every 10s, no-buffer headers, compare-at-once rule)
plus the fixes below, with a local uvicorn and the live store (`SG`). "Longest gap" is the
longest silence between SSE events. If it never reaches 10s, no keep-alive is needed. The
keep-alive itself is covered by `test_chat_stream_sends_keepalives_while_a_turn_is_quiet`.

| # | Check | Result | Components | Turn time | Longest gap | Errors |
|---|---|---|---|---|---|---|
| i1 | One session: "Plan a 2-day trip", then `/api/cart/add` | **Pass** | `itinerary` (City Pass, Universal Studios), `suggestions`; the add returned 200 | 12.7s | 2.3s | none |
| i2 | Same session: chip "Compare two tours" | **Pass**, no "which two?" question | `comparison` (KL City Tour vs Phuket Island Hopping) | 10.6s | 5.2s | none |
| i3 | Same session: "Mt Fuji vs Kuala Lumpur" | **Pass** | `comparison` (Mt Fuji Day Trip vs KL City Tour). No new search was needed: both came from the trip turn's "tour" search. | 9.6s | 2.4s | none |
| i4 | Same session: "what is the travel itinerary" | **Pass** | `itinerary`: Day 1 City Pass (in the cart), Day 2 Universal Studios, with an "Add Universal Studios ticket" chip | 8.6s | 3.7s | none |
| i5 | Tee search → sizes → "Add a size M" | **Pass** | `products`, then `get_product_details` + `add_to_cart`; cart: Tee M ×1 | 7.9s / 8.0s | 1.9s | none |
| i6 | "Make it 2 of the tee", "Remove the eSIM" | **Fail, then fixed** | Before the fix, the remove left the eSIM in the cart (`cart_update` still had it, subtotal 79.70) while the agent said "Removed". After: cart Tee M ×2, SGD 59.80. | 5.2–6.6s | 2.0s | none |
| i7 | "compare your mobile plans" | **Pass** | `plan_matrix` (Starter 30GB vs Plus 100GB) | 14.1s | 2.0s | none |
| i8 | "how many days to return?" / "are tickets refundable?" | **Pass** | `search_policies` grounded: 30 days, 7 business days for refunds; tickets non-refundable | 6.3s / 3.8s | 1.6s | none |
| i9 | "remember I am vegetarian" → "suggest a food tour" | **Pass** | `save_memory`, then `products`. The reply warns that the street-food tours are usually meat-heavy and suggests checking with the operator. | 6.0s / 8.1s | 1.5s | none |
| i10 | Checkout via chat and via `/api/checkout` | **Pass** | Both `checkout` cards had a `https://iknowledge-dev.myshopify.com/cart/c/…` link | 8.4s / 1.0s | 3.3s | none |
| i11 | "Tell me about gid://shopify/Product/1" | **Pass after a fix** | Before: "That product lookup isn't working right now". After: "I couldn't find a product with that ID… it may be mistyped". The next turn ("do you have a mug?") worked normally. | 6.9s | 1.8s | none |

No turn emitted an `error` event or dropped. The slowest turn took 14.1s, and the longest
silence between events was 5.2s. The keep-alive still matters on the phone path through
Render's proxy; this local run could not test that path.

#### i12: the in-chat product page

| Check | Result | Notes |
|---|---|---|
| `POST /api/product`, Logo Tee | **Pass** | 1 image (the store has one per product), description, variants S and M, product URL. Adding variant S returned 200. |
| `POST /api/product`, Mt Fuji Day Trip | **Pass** | 1 image, description, no variants. Adding the plain product returned 200. |
| Chromium: View details → sheet → size M → Add to cart → checkout bar | **Pass** | The sheet opened with no page change (URL stayed `/`, no navigation, no JS errors). The bar read "Checkout · 1 item · SGD 29.90". Screenshot: [`product-sheet.png`](product-sheet.png) |

What looked wrong on real data:
- **Run-on sentences (fixed).** Shopify's description "html" has no tags and no space
  between sentences ("with lunch.Experience with…"). `_text` now adds the space, and a test
  covers it.
- **Store data.** Descriptions are one block of boilerplate, with "Product details …" items
  run together and a "SKU: IK-RE-TEE-S" on every tee size.
- **Store data.** The tee's options list S, M and L, but Shopify no longer returns an L
  variant, so the sheet offers only S and M while the text still says "L is sold out".
- No HTML leaked into the text, and no photos were missing.

#### i13: products on the first reply (forced first search), 10 fresh sessions

| Opener | First tool | Product component | Real ids | Time |
|---|---|---|---|---|
| I need a gift | search_products | products | 5 | 9.3s |
| help me plan a trip | search_products | plan | 3 | 20.0s |
| something fun for the weekend | search_products | products | 4 | 12.4s |
| what phone plan should I get? | search_products | plan_matrix | 2 | 11.3s |
| Compare two tours | search_products | comparison | 2 | 14.7s |
| any ideas for my mum? | search_products | products | 4 | 9.4s |
| what's good for a first-time visitor to Singapore? | search_products | products | 4 | 11.8s |
| I'm going to Japan | search_products | plan | 4 | 25.7s |
| cheap stuff under 20 | search_products | products | 4 | 11.7s |
| do you sell travel adapters? | search_products | products | 1 | 7.0s |

**All 10 of 10 showed products on the first reply.** None ended on only a question.

Control turns:
- "how many days do I have to return an item?" → `search_policies` first.
- "add it to my cart" → `add_to_cart` first, with no forced search.
- "thanks" → no tool, and the host's fallback chips.

The forced search adds a round, so the longer plans took 20–26s. Their longest gap still
stayed well under 10s.

**Found and fixed: every shopper shared one memory.**
- `chat.html` starts every chat with `{}`, so every visitor got the `user_id` "demo-user",
  and long-term memory is stored per user.
- Live, a brand-new chat answered "you're planning a trip to Japan", from another session.
  The adapter opener also said "since you're planning that trip".
- Fixed: `/api/session` gives each chat its own `guest-…` id and ignores any client-sent id.
  Without sign-in, anyone could otherwise claim someone else's id.
- After the fix, a fresh chat says it has nothing saved, and memory still works within a
  chat. A test covers it.

#### i14: chips

- **46 of 46** recorded round-(i) chat turns ended with a `suggestions` event: 45 from the
  model and 1 from the host fallback ("thanks"). None was vague ("Tell me more", "Anything
  else?").
- **First tap test: 4 of 6 chips** from real replies, sent with `"source": "chip"`, showed
  products.
- **The two failures.** "Show more Singapore activities" had nothing more to show. "Search
  for spa or wellness gifts" asked for a category the store doesn't carry. Both replies were
  honest but text only.
- **Fixes.** `DISCOVERY_NOTES` now says "Show more X" needs more X, a category chip needs a
  result in that category, and an empty search still shows the closest products. The model
  still sometimes offered such chips ("Show jewelry or accessories", "Show more Singapore
  tours"), so the host now enforces it: a search chip whose reply shows no products gets
  "Closest matches" cards above its chips. Cart and sign-off chips are excluded, and tests
  cover both cases.
- **After the fixes: 9 of 9 chip taps showed products with real ids.** That covers every
  non-"Add" chip from the mum and Singapore openers, plus the earlier failures.

#### i15: every key flow with no API credit (scripted model, live store), 2026-10-06

`NO_MODEL=1 scripts/loop.sh`, see `e2e/README.md`. Lint, 89 unit tests, live smoke,
13 API flows (47 scripted model calls, 0 errors), and a Chromium walk at 390x844:
**19/19**. The walk covered:

- discovery chip to cards, add from a card, choose size M
- the in-chat product page
- Japan vs Malaysia comparison with a facts table (Price, Duration, Includes, Delivery,
  Free cancellation, Date changes)
- the plan table and an itinerary
- the 30-day return FAQ and a mailto handoff
- the Shopify checkout link
- a dropped connection: a plain message, and Try again resends
- model outage: store products, add to cart and the FAQ answer still work
- no raw JSON, no sideways scroll, no page errors

Screenshots are in `docs/uat/e2e/`.

Fixed along the way:

- A 25-result search came to about 13k characters because of the store's repeated
  description template ("Hand-picked…", "Product details…"). That is over the runtime's
  12k fence, so the model saw truncated JSON. Summaries now stop at the template, and a
  search is about 9k.
- The comparison row was a horizontal scroller with one card visible at phone width. It is
  now a two-column grid, and only 4 or more entries scroll.

## Findings and fixes

1. **Every physical product was "sold out" at the cart (fixed by configuration).**
   - Search and `get_product` report every variant `available: true`. But `create_cart` dropped
     all 23 shipped products with `merchandise_out_of_stock` ("already sold out") whenever the
     buyer country was `US` or unset (when unset, Shopify goes by the server's IP).
   - With `address_country: SG`, all of them add, including all three tee sizes. Non-shipping
     items add under any country.
   - So the store ships only to its Singapore market. `render.yaml` set no country, which means
     the live deploy would have refused every book, mug and tee.
   - Fixed:
     - `render.yaml` now sets `SHOPIFY_BUYER_COUNTRY=SG`.
     - `create_checkout` now sends the country too. Without it, Shopify adds "can't be shipped
       to your address".
     - The backend docstring and CLAUDE.md explain the setting.
     - `tests/fake_shopify.py` models the market rule.
     - A new test covers it.
2. **Policy search returned nothing for booking, ticket and fee questions (fixed).**
   - `search_shop_policies_and_faqs` answers only questions close to the store's FAQ wording.
     "service fees", "ticket transfer", "booking" and even "refund" all return `[]`.
   - Because of that, the agent first answered "no terms on file". In fact the return policy says
     "travel bookings, telecom top-ups and tickets are non-refundable".
   - Fixed: when a policy search finds nothing, `search_policies` falls back to the store's
     return and shipping policies. The agent now answers that bookings and tickets are
     non-refundable, and that the policies say nothing on transfer, resale or service fees.
3. **Multi-word searches came back empty (fixed in the prompt).**
   - Shopify's catalog search needs every word to match: "mobile plan" finds nothing, while
     "plan" finds 3 products.
   - The first travel run spent 8 searches and told the shopper "Nothing matched…".
   - Fixed: `SHOPIFY_PROMPT_NOTES`, which `shopify_agent_config` always adds to the prompt,
     tells the agent to search one or two key words and to try a single word before giving up.
     The next run needed fewer empty searches.
4. **The checkout wording was misleading (fixed in the prompt).**
   - The reference prompt says checkout "stages a summary the customer confirms in the app", so
     the agent said "staged for you to confirm in the app".
   - Fixed: the same notes say that the "Check out securely" button opens Shopify's checkout for
     contact, delivery and payment details. Replies now say exactly that.
5. **Chat bubbles ran sentences together (fixed).**
   - Text that resumed after a tool call was appended with no break ("…experiences.Good, found").
   - Fixed: `chat.html` now starts a new paragraph after a tool call. It also no longer requests
     a missing `/favicon.ico`.
6. **The setup step failed in a fresh session (fixed).**
   - `pip install -r vendor/commerce-agents/requirements.txt` fails from the repo root because
     the file's `-e ./…` paths are relative.
   - Fixed: `setup.sh` and CLAUDE.md now install from inside the vendor directory. The Render
     build already did this.
7. **Product links use the real handle (test shape fixed).**
   - The live catalog gives each product a `handle` and no `url`, so `product_url` is
     `https://{shop}/products/{handle}`, not the storefront-search fallback.
   - The fake store had no `handle`, so tests only covered the fallback. It now has one, and
     the tests check the `/products/…` link.
8. **A product named "Transfer" forced policy reads (fixed).**
   - The ticketing terms included a bare `transfer`. The store sells an "Airport Transfer",
     so after a card tap, the app-event note naming it made "Do you have a t-shirt?" a "policy
     question". So did "Do you have an airport transfer?".
   - Fixed: `POLICY_TERMS` now uses phrases like "transfer my", "transfer a ticket" and
     "transferable". A test covers both cases.
9. **Vague first messages got a question instead of products (fixed in the prompt).**
   - The base prompt allows one clarifying question, and the model used it for "I need a gift"
     and "help me plan a trip".
   - Fixed: `DISCOVERY_NOTES` says that allowance doesn't cover a first reply, with examples
     for exactly these two requests. It also says chip prices use the catalog's currency, and
     chips only offer categories the results showed.
   - Also: "what phone plan should I get?" searched "phone plan", found nothing, and told the
     shopper the store has no phone plans. `SHOPIFY_PROMPT_NOTES` now says to retry a
     two-word query with its key word alone ("phone plan" → "plan"). Both re-runs found the
     postpaid plans.
10. **Cart edits by product id silently did nothing (fixed, round i6).**
   - A single-variant product is added by its product id, but its cart line is a variant
     id. So `remove_from_cart` and `update_cart_item` by product id matched no line and
     returned the unchanged cart, and the gate told the agent "Removed".
   - Fixed: `_line_for` maps a product id to its cart line (its default variant, or its one
     variant in the cart).
   - When two sizes of one product are in the cart, it names both and asks which one. When
     nothing matches, it says "not in the cart; nothing was changed".
   - Two tests cover this, and both fail on the old code.
11. **An unknown product id read as an outage (fixed, round i11).**
   - Live, `get_product` answers an unknown id with `isError` and code `product_not_found`.
     The backend raised a generic error, so the agent said "the lookup isn't working right
     now".
   - Fixed: `*not_found` codes now raise `ShopifyNotFound`, and `get_product_details`
     returns `None` ("No product with id …").
   - The fake store uses the live error code, and a test covers it.
12. **The fake store now matches live response shapes.**
   - Variants have no `seller`, `availability` is just `{available}`, and option values carry
     only `label`.
   - Added a non-shipping product (an e-voucher) and FAQ search that only answers close
     questions.

## Still open

- **Chip prices in dollars.** Some chips still say "under $40" or "under $50" although the
  catalog is in SGD.
- **The trip reply sometimes describes products before searching.** In round i1, the first
  sentence promised "a guided walking tour, a museum pass" before any search. The plan that
  followed used real Singapore products. This is the model's pre-tool preamble.

- **Product pages are behind the storefront password.** Every `/products/…` link (the new
  "View details" links) redirects to `/password`, so shoppers can't open them. The agent's
  API calls aren't affected, and the checkout handoff link goes straight to Shopify checkout.
  To fix, remove the password under Online Store > Preferences.
- **Discovery searches are still sometimes too long.** The Tokyo run tried "Tokyo walking
  tour" and "Tokyo temple tour" before "Tokyo", even with the short-keyword note.
- **The deploy branch is unconfirmed.** Another session reported that Render deploys from
  `claude/commerce-agents-study-rdoe5a`, not the `render.yaml` branch, and asked for pushes to
  go there too. This session pushed only to `claude/festive-sagan-q8rglv`. Confirm the branch in
  the Render dashboard before anyone pushes to the other branch. Also, raw JSON on the live
  site would come from an older build: locally, the itinerary and plan turns render as cards.

- **The owner's environment variable.** This cloud environment sets
  `SHOPIFY_BUYER_COUNTRY=US`, so a plain `python -m scripts.shopify_smoke` fails at add to cart.
  Change it to `SG` in the environment settings. Render gets `SG` from `render.yaml`.
- **Render was not checked.** To let a session test the deploy, add
  `iknowledge-shopping-agent.onrender.com` to the environment's allowed domains. Add
  `cdn.shopify.com` too: product photos are blank in the screenshot only because this sandbox
  blocks that host.
- **Store data: Logo Tee.** The description says "L is sold out", but Shopify sells L. The agent
  repeats the description to shoppers. Either fix the description or set L's inventory to 0.
  The description also says "SKU: IK-RE-TEE-S" for every size.
- **Store data: plan rows.** The two postpaid plans have identical descriptions, and their
  allowance (100GB vs 30GB) is only in the title. The comparison table therefore has just Price
  and Available rows. Adding data allowance and minutes as product options or metafields would
  give the table real rows.
- **Model slip at checkout (1 of 3 runs).** In one run, the checkout note said the cart held
  "two" tees when it held one. The checkout card and the cart showed 1, so the shopper still saw
  the right quantity. Two repeat runs were correct. This comes from the model, not from cart data
  (`get_cart` returned quantity 1). Watch for it.
- **Malformed tool JSON (1 run).** In one travel run the model sent invalid JSON for
  `present_plan`. The reference runtime asked again and the retry worked.
- **Suggestion chips.** Earlier runs offered things the store doesn't carry ("Browse water
  sports gear" after the surfboard answer). After the fix, the scenario (g) and (h) chips all
  pointed at carried products or categories. One gift run still offered "Gift under $30"
  although prices are in SGD.
