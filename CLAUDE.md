# claude-commerce-agents

A study guide and starter kit for `anthropics/commerce-agents` (the reference shopping and
merchant agents). The owner is connecting the shopping agent to their own Shopify store.
`README.md` explains everything; this file is the working brief.

## State

- `my_store/backend.py`: sample store over `catalog.json` (works offline).
- `my_store/shopify_backend.py`: `ShopifyUCPBackend`, the shopping agent's `StorefrontBackend`
  over a Shopify store's UCP tools (`https://{shop}/api/ucp/mcp`) and policy tool
  (`https://{shop}/api/mcp`). Built from shopify.dev docs; tested only against
  `tests/fake_shopify.py`, **not yet against a live store**.
- `my_store/app.py`: FastAPI host; `STORE_BACKEND=shopify` selects the Shopify backend.

## Setup in a fresh session

```bash
git clone --depth 1 https://github.com/anthropics/commerce-agents vendor/commerce-agents
python3 -m venv .venv && source .venv/bin/activate
pip install -q -r vendor/commerce-agents/requirements.txt
python -m pytest -q                      # tests against the simulated store
```

## "Test Shopify" request

The owner sets `SHOPIFY_STORE_DOMAIN` and `STORE_AGENT_ANTHROPIC_API_KEY` as environment
variables (cloud environments drop `ANTHROPIC_API_KEY`; `my_store/app.py` copies the other
name over) and
allows `*.myshopify.com`, `api.shopify.com`, `api.anthropic.com` in network access. Then:

1. `python -m scripts.shopify_smoke --query "<word matching their products>"`; if search
   finds nothing, ask what the store sells.
2. Fix any mismatch between real responses and the parsing in `shopify_backend.py`
   (most likely: cart `line_items` in `_cart`, the policy answer in `search_policies`,
   `get_product` variants). Update `tests/fake_shopify.py` to the real shape so the tests
   keep matching, then `ruff check . && ruff format --check . && python -m pytest -q`.
3. With `STORE_AGENT_ANTHROPIC_API_KEY` (or `ANTHROPIC_API_KEY`) set, run one live chat through `my_store/app.py`
   (`STORE_BACKEND=shopify`): search, pick a size, add, ask about returns, check out.
4. Report plainly what passed and what failed; commit and push fixes to the working branch.

A storefront password on a development store may block agent access; if `.well-known/ucp`
or search fails with a password or 401 page, ask the owner to remove the password
(Online Store > Preferences). Never ask for keys in chat; they belong in environment settings.

## Conventions

Python 3.11+, `ruff` (`ruff.toml`), `pytest` (`pytest.ini`). Commit with clear messages;
do not open pull requests unless asked.
