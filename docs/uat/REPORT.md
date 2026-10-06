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
10. **The fake store now matches live response shapes.**
   - Variants have no `seller`, `availability` is just `{available}`, and option values carry
     only `label`.
   - Added a non-shipping product (an e-voucher) and FAQ search that only answers close
     questions.

## Still open

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
