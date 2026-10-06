# Launch the shopping assistant on your Shopify store

The agent runs on Render (Shopify can't run Python apps). Your store loads one script that
adds a chat bubble. About 15 minutes, all in the browser.

## 1. Deploy to Render

1. Sign up at <https://render.com> with your GitHub account.
2. **New > Blueprint**, pick the `claude-commerce-agents` repo. Render reads `render.yaml`
   and proposes one web service, `iknowledge-shopping-agent`, on the Free plan (no card).
   It sleeps after about 15 minutes without visitors; the next reply then takes 30-60
   seconds and open chats are cleared. To keep it always on, change `plan: free` to
   `plan: starter` in `render.yaml` (about $7/month).
3. It asks for the secret values. Paste them there, never into the repo or a chat:
   - `ANTHROPIC_API_KEY`: from <https://console.anthropic.com> > API keys.
   - `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`: the same pair you set for testing. Without
     them, checkout uses the cart's own Shopify link and still works.
4. **Apply**. The first build takes a few minutes. When it is live, open
   `https://<your-service>.onrender.com/` and send a message: this is the full-page chat.
5. **Set a spend limit on the API key's workspace** at console.anthropic.com (Settings >
   Limits). The app's own limits below live in its memory and reset whenever Render
   restarts it (the free plan sleeps when idle), so this is the ceiling that always holds.

Render deploys again on every push to the branch in `render.yaml`
(`claude/festive-sagan-q8rglv`). Change `branch:` there after you merge to `main`.

## 2. Add the chat bubble to your store

1. Shopify admin > **Online Store > Themes**. On your current theme: **⋯ > Edit code**.
2. Open `layout/theme.liquid`. Just above `</body>` paste (with your Render address):

   ```html
   <script src="https://<your-service>.onrender.com/widget.js" defer></script>
   ```

3. **Save**, then open your storefront: a round chat button sits at the bottom right.
   To try it before shoppers see it, do this on a duplicate of the theme and use **Preview**.

To remove it, delete that line. The bubble shows only on the stores in `WIDGET_SHOPS`
(by default the `SHOPIFY_STORE_DOMAIN` this app sells from), so the same theme on a trial
store shows no chat. To give a trial store its own assistant, deploy a second Render service
with `SHOPIFY_STORE_DOMAIN` set to the trial store and point that store's script tag at it.

## 3. Check before telling anyone

- Search, add to cart, ask "how many days do I have to return an item?", check out. The
  checkout button opens Shopify checkout in the same tab.
- Physical products: the app asks the store which countries it ships to (`/meta.json`)
  and uses its country when `SHOPIFY_BUYER_COUNTRY` is unset or not one of them. If items
  still won't add, check each product's **Inventory** and the store's shipping markets.
- "I want to talk to a human" should answer with the contact email (`STORE_CONTACT_EMAIL`).
- Development store: real customers can't pay until the store is on a paid plan. Moving to
  the live store later means changing `SHOPIFY_STORE_DOMAIN` in Render's **Environment** tab.

## Limits (Render > Environment to change)

| Setting | Default | What it does |
| --- | --- | --- |
| `TOKENS_PER_DAY` | 10000000 | model tokens per day across all shoppers, weighted to input-token cost (output ×5); about US$30/day at US$3 per million input tokens. New chats wait for the next day once it is spent |
| `TURNS_PER_DAY` | 1500 | messages per day across all shoppers |
| `TURNS_PER_IP_PER_DAY` | 150 | messages per day from one IP, so one visitor can't use the day up |
| `CHAT_PER_MINUTE` | 10 | messages per minute from one shopper's IP |
| `SESSIONS_PER_HOUR` | 60 | new chats per hour from one IP (a mobile carrier or an office shares one IP across many shoppers) |
| `TURNS_PER_SESSION` | 40 | messages in one chat |
| `SESSION_IDLE_MINUTES` | 120 | idle chats are dropped |
| `METRICS_TOKEN` | (unset) | set it to read the conversion funnel at `/api/metrics?token=...`; unset, that page does not exist |
| `CHECKOUT_UTM_SOURCE` | (unset) | e.g. `assistant`: tags checkout links so Shopify's reports show orders that came through the chat |
| `STORE_CONTACT_EMAIL` | tanyueting96@gmail.com | where the assistant sends shoppers who ask for a person |
| `LOAD_TEST_TOKEN` | (unset) | for eval crawls and load tests from one machine: requests sending the same value in an `X-Load-Test-Token` header skip the per-IP limits above (`CHAT_PER_MINUTE`, `SESSIONS_PER_HOUR`, `TURNS_PER_IP_PER_DAY`); the daily turn and token budgets still apply. Unset it after testing |
| `CATALOG_REFRESH_TOKEN` | (unset) | set it to re-read the catalog on demand: `POST /api/catalog/refresh` with header `X-Refresh-Token` (e.g. from a Shopify Flow "Product created" HTTP action). Unset, that endpoint does not exist |
| `CATALOG_WARMUP` | 1 | read the whole catalog (a few Shopify calls) at boot and every 10 minutes in the background: opening chips from collections, "what do you sell", price rankings, and search answers while Shopify's search fails |
| `WIDGET_SHOPS` | `SHOPIFY_STORE_DOMAIN` | myshopify domains where the chat bubble shows (comma-separated); on any other store, a trial store for example, it stays hidden |

## Is it healthy?

`GET /healthz` shows the deployed commit (`commit`, from Render's `RENDER_GIT_COMMIT`) and
the store's health: `degraded` (several catalog calls failed lately and none worked since),
`failures_last_5m`, `last_error`, `catalog_products`, `catalog_synced_seconds_ago`
(the index is re-read every 10 minutes; products found by live search join it at once), and `buyer_country` (should be `SG`
for iKnowledge; anything else means shipped items get refused). Render's logs show each
Shopify retry as `shopify HTTP 429, retry n` (430 is Shopify's bot protection), and each
search answered from the catalog index while Shopify fails.

At most 4 Shopify calls run at once across all chats, and a search makes one Shopify call
(a country's cities and a word's synonyms are matched in the catalog index). Timeouts and
dropped connections are retried once. When Shopify throttles the server (429, or 430 bot
protection), catalog calls pause for 10 s, doubling while it repeats, up to 2 minutes
(`catalog_paused_seconds`); the first success resets it. Searches are
answered from the catalog index meanwhile, so shoppers still see products. If the server
boots while throttled, the index starts from `my_store/catalog_snapshot.json`; refresh it
with `python -m scripts.catalog_snapshot` after catalog changes.

A burst from one IP hits this server's own per-IP limits first: the 61st new chat in an
hour gets "Too many new chats from your connection" (HTTP 429). For a crawl, set
`LOAD_TEST_TOKEN` and send it as `X-Load-Test-Token`.

## Known limits of this first launch

- Chats live in the server's memory: a redeploy or restart clears open chats (the cart in
  Shopify is not lost, but the assistant forgets it). Fine on one instance; add Redis before
  running more than one.
- `UCP_AGENT_PROFILE_URL` still points at Shopify's example agent profile. Host your own
  profile before a full public launch (shopify.dev/docs/agents).
