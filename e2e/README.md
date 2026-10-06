# End to end with no API call

The evals (`evals/`) judge what the model chooses and cost API credit. This suite checks
everything else a shopper depends on, for free: the real app, runtime, gates, the live
Shopify store, and the chat page, with `scripted_model.py` standing in for Claude. Each
shopper message is matched to a scripted policy that reads the live tool results of the
turn, so the ids it adds or compares are the store's real ids.

```bash
python -m e2e.run_api                         # 17 flows over /api/chat, in-process
python -m scripts.e2e_server &                # the app on :8000, scripted model
node e2e/shopper.js "$(npm root -g)" docs/uat/e2e   # Chromium at phone width, screenshots
NO_MODEL=1 scripts/loop.sh                    # lint, tests, smoke, both of the above
```

Needs `SHOPIFY_STORE_DOMAIN` (and `SHOPIFY_BUYER_COUNTRY` if the store's `meta.json`
doesn't give it); no Anthropic key. `/e2e/outage/1` (test server only) makes every model
call fail like an out-of-credit account, to check the no-model fallback.

| Flow | `run_api.py` | `shopper.js` |
|---|---|---|
| First reply shows products; chip -> cards | yes | yes |
| Product options; choose a size on the card | yes | yes |
| Cart by chat: add variant, change qty, remove; cap of 8 | yes | |
| Add to cart from a card; checkout bar | | yes |
| Unseen product id refused (provenance gate) | yes | |
| Compare across countries: a value in every cell, a sentence with the card | yes | yes |
| "Compare these two" = the products last shown | yes | yes |
| "Difference between X and Y" -> a comparison card | yes | |
| "Compare this with the mug" on a product page | yes | |
| A comparison with an unseen id is refused, then retried | yes | |
| Plan table (telecom), itinerary (travel) | yes | yes |
| Policy from the store's FAQ; human handoff (mailto) | yes | yes |
| Memory; checkout with a cart and with none | yes | |
| Shopify checkout link | yes | yes |
| Dropped connection: plain message, Try again resends | | yes |
| Model outage: store products, add to cart, FAQ answer | | yes |
| No raw JSON, no sideways scroll, no page errors | | yes |
