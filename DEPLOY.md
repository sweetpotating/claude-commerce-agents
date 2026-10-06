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
| `SESSIONS_PER_HOUR` | 20 | new chats per hour from one IP |
| `TURNS_PER_SESSION` | 40 | messages in one chat |
| `SESSION_IDLE_MINUTES` | 120 | idle chats are dropped |
| `STORE_CONTACT_EMAIL` | tanyueting96@gmail.com | where the assistant sends shoppers who ask for a person |
| `WIDGET_SHOPS` | `SHOPIFY_STORE_DOMAIN` | myshopify domains where the chat bubble shows (comma-separated); on any other store, a trial store for example, it stays hidden |

## Known limits of this first launch

- Chats live in the server's memory: a redeploy or restart clears open chats (the cart in
  Shopify is not lost, but the assistant forgets it). Fine on one instance; add Redis before
  running more than one.
- `UCP_AGENT_PROFILE_URL` still points at Shopify's example agent profile. Host your own
  profile before a full public launch (shopify.dev/docs/agents).
