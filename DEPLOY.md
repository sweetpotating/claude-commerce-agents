# Launch the shopping assistant on your Shopify store

The agent runs on Render (Shopify can't run Python apps). Your store loads one script that
adds a chat bubble. About 15 minutes, all in the browser.

## 1. Deploy to Render

1. Sign up at <https://render.com> with your GitHub account.
2. **New > Blueprint**, pick the `claude-commerce-agents` repo. Render reads `render.yaml`
   and proposes one web service, `iknowledge-shopping-agent` (Starter plan, about $7/month).
3. It asks for the secret values. Paste them there, never into the repo or a chat:
   - `ANTHROPIC_API_KEY`: from <https://console.anthropic.com> > API keys.
   - `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`: the same pair you set for testing. Without
     them, checkout uses the cart's own Shopify link and still works.
4. **Apply**. The first build takes a few minutes. When it is live, open
   `https://<your-service>.onrender.com/` and send a message: this is the full-page chat.
5. Set a spending limit at console.anthropic.com > Billing as a second guard.

Render deploys again on every push to the branch in `render.yaml`
(`claude/commerce-agents-study-rdoe5a`). Change `branch:` there after you merge to `main`.

## 2. Add the chat bubble to your store

1. Shopify admin > **Online Store > Themes**. On your current theme: **⋯ > Edit code**.
2. Open `layout/theme.liquid`. Just above `</body>` paste (with your Render address):

   ```html
   <script src="https://<your-service>.onrender.com/widget.js" defer></script>
   ```

3. **Save**, then open your storefront: a round chat button sits at the bottom right.
   To try it before shoppers see it, do this on a duplicate of the theme and use **Preview**.

To remove it, delete that line.

## 3. Check before telling anyone

- Search, add to cart, ask "how many days do I have to return an item?", check out. The
  checkout button opens Shopify checkout in the same tab.
- Physical products: if the assistant says they are sold out, give them stock (or tick
  "Continue selling when out of stock") under each product's **Inventory**.
- Development store: real customers can't pay until the store is on a paid plan. Moving to
  the live store later means changing `SHOPIFY_STORE_DOMAIN` in Render's **Environment** tab.

## Limits (Render > Environment to change)

| Setting | Default | What it does |
| --- | --- | --- |
| `TURNS_PER_DAY` | 1500 | messages per day across all shoppers; the ceiling on the Anthropic bill |
| `CHAT_PER_MINUTE` | 10 | messages per minute from one shopper's IP |
| `SESSIONS_PER_HOUR` | 20 | new chats per hour from one IP |
| `TURNS_PER_SESSION` | 40 | messages in one chat |
| `SESSION_IDLE_MINUTES` | 120 | idle chats are dropped |

## Known limits of this first launch

- Chats live in the server's memory: a redeploy or restart clears open chats (the cart in
  Shopify is not lost, but the assistant forgets it). Fine on one instance; add Redis before
  running more than one.
- `UCP_AGENT_PROFILE_URL` still points at Shopify's example agent profile. Host your own
  profile before a full public launch (shopify.dev/docs/agents).
