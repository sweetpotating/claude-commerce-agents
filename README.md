# Studying Claude's commerce agents

A study guide and starter kit for [`anthropics/commerce-agents`](https://github.com/anthropics/commerce-agents),
with a working "bring your own store" shopping agent that goes from **discovery to checkout**.

```bash
./setup.sh                                  # clones the reference into vendor/, installs it into .venv
source .venv/bin/activate
python -m scripts.walkthrough               # offline, no API key: discovery -> cart -> checkout -> order
export ANTHROPIC_API_KEY=...                # then the real agent:
uvicorn my_store.app:app --port 8000
```

Sample output of the walkthrough: [`docs/walkthrough-output.txt`](docs/walkthrough-output.txt).

---

## 1. What the reference repo is

Two agents, each defined once (prompt, skills, tool contracts, safety gates) and runnable on
three runtimes:

| Agent | Who uses it | Does |
|---|---|---|
| **Shopping agent** | Your customers, embedded in your app | Search, compare, plan multi-item purchases, fill the cart, stage checkout, order status, returns/policy Q&A, remembers preferences |
| **Merchant agent** | Your staff | Explains performance, fixes listings, restocks, pricing/promos, drafts campaigns. Every write is a *staged change* a human approves |

| Runtime | Who runs the loop | Use when |
|---|---|---|
| Messages API (`ShoppingAgent.stream_turn`) | Your server | Reference path; full grounding + memory extraction. **Start here.** |
| Claude Agent SDK | The SDK | You already build on the Agent SDK |
| Managed Agents | Anthropic-hosted | You'd rather not host the loop; you host an MCP server over your backend |

Four runnable verticals (`examples/retail`, `travel`, `telecom`, `entertainment`) each ship a
FastAPI API, a Next.js storefront with generative UI, and a merchant portal:
`python scripts/run_demo.py retail` in the reference repo (needs Node 22 + an API key).

### The shopping agent's five skills (flows)

Loaded on demand via a `load_skill` tool, so they don't bloat every prompt:

| Skill | Handles |
|---|---|
| `search-discovery` | A described need → 3-6 option shortlist with a recommended pick |
| `purchase-research` | "What should I look for in X?" → criteria from your buying guide, then options |
| `planning-goals` | "Outfit a camping trip for 4" → a multi-step plan with one item per step |
| `customer-care` | Order status, returns, refunds, damaged items, grounded in your policies |
| `memory-personalization` | Remember / recall / forget customer facts |

### The tools the model gets

Reads: `search_products`, `get_product_details`, `get_cart`, `get_preferences`, `get_orders`,
`get_order_status`, `search_policies`, `get_fulfillment_options`, `recall_memories`.
Writes: `add_to_cart`, `update_cart_item`, `remove_from_cart`, `save_memory`.
UI (generative components streamed as `ui` events): `present_products`, `present_comparison`,
`present_plan`, `present_guide`, `present_order_status`, `present_suggestions`, and **`checkout`**.

## 2. The discovery → checkout pipeline

```
customer msg ─► your host (/api/chat) ─► ShoppingAgent.stream_turn ─► Claude
                                                │ tool calls
                                                ▼
                                  ShoppingToolExecutor  (gates: provenance, options, caps, fencing)
                                                │
                                                ▼
                                  YOUR StorefrontBackend ─► your search / cart / OMS / CMS
                                                │
            SSE events ◄── text_delta · ui(products|comparison|checkout) · cart_update · turn_complete
```

| Stage | Model calls | Your backend method | Guardrail that holds in code |
|---|---|---|---|
| Discovery | `search_products` → `present_products` | `search_products` | Results fenced as untrusted data; result count clamped |
| Research | `get_product_details`, `present_comparison` | `get_product_details` | UI payloads re-joined from server records; unseen ids dropped |
| Variant choice | `get_product_details` | variants in details | **Options gate**: can't add a family (e.g. "the tee"), must pick a size variant |
| Cart | `add_to_cart` | `add_to_cart` | **Provenance gate**: only ids returned this session; per-item / line caps; `Unavailable` relayed with in-stock siblings |
| Delivery | `get_fulfillment_options` | `get_fulfillment_options` | — |
| Checkout | `checkout` | `checkout_handoff` | **No payment** in the agent: card shows the cart; the hosted-checkout URL is added server-side and never seen by the model |
| Payment | — (your app) | your webhook | You queue an "app event" so the next turn knows the order exists |
| Post-purchase | `get_order_status`, `search_policies` | `get_order(s)`, `search_policies` | Grounding forces a read before answering order/policy questions |

The walkthrough script plays exactly this sequence, including each gate saying no.

## 3. Connecting your own store

You implement **one class**: `StorefrontBackend`. That's the whole integration surface.
`my_store/backend.py` is a complete implementation backed by `catalog.json`; each method has a
`TODO(live)` naming what to call instead.

| Method | Map to | Required |
|---|---|---|
| `search_products(session, query, filters, limit)` | Search engine / platform search API | yes |
| `get_product_details(session, product_id)` | PDP / product API, including `variants` | yes |
| `get_cart` / `add_to_cart` / `update_cart_item` / `remove_from_cart` | Cart API (enforce stock & eligibility here) | yes* |
| `get_preferences(session)` | Profile / CRM (guest profile if anonymous) | yes |
| `get_orders` / `get_order` | OMS, scoped to `session.user_id` | yes* |
| `search_policies(session, query)` | CMS / help center | yes* |
| `get_fulfillment_options(session, ids)` | Shipping rates / pickup | yes* |
| `checkout_handoff(session, cart)` | Create a hosted checkout session, return its URL | optional |
| `get_account_context`, `get_disclosure` | Account facts; regulated fact boxes | optional |

\* A system you don't have at all is switched off in config instead (`enable_cart`,
`enable_orders`, `enable_policies`, `enable_fulfillment`), which removes its tools and
prompt lines. A system that exists but isn't wired yet can just raise; the tool reports
"temporarily unavailable".

### Step-by-step

1. **Map your catalog** onto `Product`. Parent/child SKUs become a *family* (`options`) with
   *variants* (`option_values`, `variant_of`). See `vendor/commerce-agents/docs/backends.md`, Step 4.
2. **Bind identity at session start.** Your host authenticates the user and creates a session
   with `user_id`. No tool argument ever carries a user id. Keep per-user credentials beside
   the session, never in the prompt.
3. **Wire the cart** to your cart API. The executor already enforces provenance and caps; your
   API still enforces business rules atomically.
4. **Choose a checkout handoff**:
   - checkout is a route in your app → do nothing; the card links there;
   - platform hosted checkout (Shopify `checkoutUrl`, Stripe Checkout Session,
     commercetools, etc.) → return its URL from `checkout_handoff`;
   - marketplace → return one `CheckoutHandoff(seller=...)` per seller.
5. **Close the loop on payment**: your payment webhook records the order and queues an app
   event on the session (`my_store/app.py: /webhooks/checkout-complete/{token}`), so the next
   turn can say "your order ORD-123 is placed" and answer status questions.
6. **Configure identity & voice**: `ShoppingAgentConfig(brand_name=..., assistant_name=...,
   brand_voice=...)`; extend grounding lexicons for your domain; add a domain UI component
   with a `PresentationExtension` if needed.
7. **Render events** in your frontend: `text_delta` (stream text), `ui` (render the
   component named in `component`), `cart_update` (refresh the cart badge). The reference
   `examples/web-shared/` has React components for every built-in.
8. **Before production** (`docs/safety.md`, "What a deployment owns"): real auth, a durable
   session + memory store, rate limits, webhook signature checks, memory retention/deletion.

### Faster: let Claude Code scaffold it

```bash
claude plugin marketplace add anthropics/commerce-agents
claude plugin install commerce-builder@claude-commerce-agents
claude
/scaffold-commerce-agent a shopping assistant for our store on <your platform>
```

It interviews you about your stack, plays back a plan, and generates the backend, host, and
evals; `/add-commerce-flow`, `/author-commerce-evals`, and `/review-commerce-agent` follow on.

## 4. Running on a Shopify store

`my_store/shopify_backend.py` is a `StorefrontBackend` over one store's Universal Commerce
Protocol (UCP) tools. Turn it on with environment variables (see `.env.example`):

```bash
export STORE_BACKEND=shopify SHOPIFY_STORE_DOMAIN=your-store.myshopify.com
export ANTHROPIC_API_KEY=...
uvicorn my_store.app:app --port 8000
```

| Agent capability | Shopify tool | Needs |
|---|---|---|
| Search, product details, sizes/colours | `search_catalog`, `get_product` on `https://{shop}/api/ucp/mcp` | Agent profile URL only |
| Cart | `create_cart`, `get_cart`, `update_cart`, `cancel_cart` | Agent profile URL only |
| Checkout link | `create_checkout` → `continue_url` | `SHOPIFY_CLIENT_ID`/`SECRET`; without them the cart's own `continue_url` is used |
| Returns, shipping, FAQ questions | `search_shop_policies_and_faqs` on `https://{shop}/api/mcp` | Nothing |
| Order history, delivery options | Switched off (`shopify_agent_config()`) | Order MCP only sees orders the agent itself completed; delivery is chosen in checkout |

The buyer always pays on Shopify's own checkout page. Every call carries your agent
profile: the default is Shopify's example profile, which is fine for development; host your
own (shopify.dev/docs/agents/profiles) before going live.

`tests/fake_shopify.py` is a stand-in store built from the request and response examples
on shopify.dev; `pytest` runs the backend against it through the agent's real tool
executor. It has not been run against a live store yet: do that first (below).

### First live test checklist

1. Make a Shopify development store (free Partner account) and add a few products, one
   with sizes or colours.
2. `curl https://your-store.myshopify.com/.well-known/ucp` returns the store's UCP profile.
3. Run the app with `STORE_BACKEND=shopify` and ask for a product, pick a size, add it, and
   ask to check out. The checkout card's link should open Shopify checkout with the cart.
4. If something fails, the server log names the Shopify tool and its error message; the
   parsing that is most likely to need adjusting is cart line items (`_cart`) and the
   policy answer (`search_policies`).
5. Add `SHOPIFY_CLIENT_ID`/`SECRET` from Dev Dashboard → Catalogs → API key and re-test
   checkout (now through `create_checkout`).

## 4a. Cost and daily limits

The deployed chat runs on Claude Haiku 4.5 ($1 per million input tokens, $5 per million
output). Set `STORE_AGENT_MODEL=claude-sonnet-5` for the stronger model at twice the price.
Two daily caps keep the bill bounded. Both reset at midnight UTC and on a restart. Change
them in Render > Environment:

| Setting | Default | What it caps |
| --- | --- | --- |
| `TURNS_PER_DAY` | 1500 | Messages across all shoppers per day. A chat is usually 3-6 messages, so this is roughly 250-500 chats a day. |
| `TOKENS_PER_DAY` | 10000000 | Model tokens per day, weighted to input cost (output counts 5x). That is about US$10 a day on Haiku 4.5, or US$20 on Sonnet 5. |
| `TURNS_PER_IP_PER_DAY` | 150 | Messages from one IP, so one visitor can't use up the day |

When either daily cap is reached, new messages get "The assistant is resting for today".
Cards already shown, the cart and checkout keep working. As the ceiling that survives restarts,
set a monthly spend limit on the API key's workspace in the Anthropic Console. All the
limits are listed in `DEPLOY.md`.

## 5. Files in this repo

| Path | What |
|---|---|
| `my_store/backend.py` | `MyStoreBackend(StorefrontBackend)`: the file you edit to connect your systems |
| `my_store/catalog.json` | Fictional sample data: a tee with sizes (one out of stock), tents, a pad, policies, an order |
| `my_store/shopify_backend.py` | `ShopifyUCPBackend`: the same interface over a Shopify store's UCP tools |
| `my_store/app.py` | Minimal FastAPI host: `/api/session`, `/api/chat` (SSE), `/api/cart`, payment webhook; `STORE_BACKEND=shopify` switches backends |
| `tests/` | `ShopifyUCPBackend` tests against a simulated Shopify store (`pytest`) |
| `.env.example` | Every setting, with the Shopify ones |
| `scripts/walkthrough.py` | Offline e2e run through the real executor and gates |
| `docs/walkthrough-output.txt` | What that run prints |
| `setup.sh` | Clones the reference into `vendor/` and installs it |

## 6. Reading order for the reference repo

1. `README.md`, then `docs/backends.md` (integration), `docs/safety.md` (what's enforced)
2. `shopping-agent/core/shopping_agent/backend.py` and `types.py` (the contract)
3. `shopping-agent/core/shopping_agent/tools/registry.py` (what the model can do)
4. `shopping-agent/skills/*/SKILL.md` (how it behaves per flow)
5. `shopping-agent/runtime-messages-api/shopping_agent_runtime/orchestrator.py` (the turn loop)
6. `examples/retail/api/mock_retail.py` + `examples/demo_common/storefront.py` (a full host)

The reference is published as-is (not maintained, no contributions), Apache-2.0.
