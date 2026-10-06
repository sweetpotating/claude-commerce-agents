# Evals: is the assistant selling well, and did a change make it better?

The suite drives the real app (`my_store/app.py`) in-process against the live Shopify store
and the real model, the way a shopper's browser does: chat turns over `/api/chat`, and
card taps over `/api/cart/add`, `/api/product/options` and `/api/checkout`. Each case
starts a fresh chat. It follows the reference repo's eval method
(`vendor/commerce-agents/plugins/commerce-builder/skills/commerce-evals/SKILL.md`):
JSON cases, code graders on the streamed events, and a rubric judge only for what code
can't check.

```bash
python -m evals.runner                         # all cases, 1 trial (~10 min, ~$2)
python -m evals.runner --tags conversion -n 3  # one slice, 3 trials each
python -m evals.runner --ids cart-005 -n 5     # re-run a failure until you trust the fix
python -m evals.audit                          # check the judge before trusting it
scripts/loop.sh                                # lint -> unit tests -> live smoke -> evals
```

Results go to `evals/results/<stamp>/` (`results.jsonl`, `traces/` with every turn's tool
calls, cards and cart, `summary.json`, `summary.md`), and `evals/results/latest.md` always
holds the last summary. The runner exits 1 when a critical case fails, or when a case that
passes in `evals/baseline.json` now fails.

## What it measures

| Number | What it means |
|---|---|
| **Conversion: checkout reached / goal met** | Simulated shoppers (persona cases) chat, tap chips and Add to cart, and open checkout. This is the share that reached a Shopify checkout link, and the share whose cart also held the item they came for. |
| **Median turns to checkout** | Fewer is better: how quickly a shopper gets from hello to a checkout link. |
| **First reply shows products** | For vague openers (`first_reply` tag): the share whose first reply puts real products on screen instead of a question. |
| Pass rate, by flow and by priority | Every case's checks and rubric. `critical` failures fail the run. |
| Latency per turn (median, p90) | Seconds per reply, since slow replies lose shoppers. |
| Cost | Agent + judge + simulated shopper tokens, at list prices. |

The live counterpart is the funnel in the app (`my_store/funnel.py`, `GET /api/metrics`):
chats → products shown → added to cart → checkout shown → checkout clicked. Read the
evals to decide whether a change is safe, and the funnel to see what shoppers actually do.

## Cases

`cases/<flow>.json` holds a list of cases. Example:

```json
{"id": "cart-005-remove", "priority": "critical", "tags": ["cart"],
 "state": {"seen": ["Bookworm Ceramic Mug", "Japan eSIM"],
           "cart": [{"title": "Bookworm Ceramic Mug"}, {"title": "Japan eSIM"}]},
 "turns": ["Remove the eSIM"],
 "expected": {"cart_contains": ["bookworm ceramic mug"], "cart_not_contains": ["esim"]}}
```

- `state` sets the preconditions before the graded turns. `page` is the storefront page the
  chat opens on, `seen` lists products found by title and looked up, and `cart` holds
  lines added with the card button, optionally with an `option` such as `{"Size": "M"}`.
- `turns` are the shopper's messages. A persona case instead has `persona`, `opening`,
  `max_turns` and `goal_items`, and a simulated shopper (`judge.py`) plays it.
- `expected` keys are code graders (`graders.py`): `first_tool`, `calls_tool`,
  `calls_one_of`, `never_calls`, `ui_components`, `ui_any`, `no_ui_components`,
  `products_shown_min`, `products_shown_title`, `products_shown_title_any`, `products_not_shown_title`,
  `cart_contains`, `cart_not_contains`, `cart_quantity`, `cart_item_count`,
  `checkout_handoff`, `reply_includes`, `reply_includes_any`, `reply_omits`,
  `max_tool_calls`, `max_latency_s`. The `rubric` key goes to the judge: one PASS
  condition and one FAIL condition, naming the store fact that decides it.
- Every case also gets three global checks: no error event, every reply ends on chips,
  and no vague chips ("Tell me more").
- Products are matched by title, not id, so a case survives catalog edits. When the
  store's data changes (prices, the FAQ), update the facts in the cases that name them.

Authoring rules from the reference skill:
- Every positive case has a negative counterpart. For example, `discovery-008` checks
  that "thanks" forces no search.
- When a live run takes another route and still answers correctly, widen the case
  (`calls_one_of`, `ui_any`) rather than re-pinning it to the route you observed.

## The improvement loop

1. **Find a failure.** Look at `latest.md` after a run, a dip in a funnel step
   (`/api/metrics`), or a complaint from a shopper.
2. **Pin it as a case** in the right flow file before fixing, and watch it fail:
   `python -m evals.runner --ids <id> -n 3`.
3. **Fix the cause** in the backend (`shopify_backend.py`), the host (`app.py`,
   `executor.py`, `discovery.py`), the instructions (`industry.py`, `my_store/skills/`) or
   the UI (`static/chat.html`). Add a unit test when the fix is code.
4. **Re-run that case with `-n 3`**, then `scripts/loop.sh`. Read the newly failing list:
   a case that breaks means the change broke a behaviour, or the case encoded a stale
   one. Fix whichever it is, and say which in the commit.
5. **Promote** with `python -m evals.runner --update-baseline` once the run is clean and its
   conversion numbers are at least the baseline's. Commit `evals/baseline.json` with the
   change.

Read differences between runs as failure sets, not topline points: one trial of a model
turn varies. Use `-n 3` before calling something fixed or broken.

**Running it automatically.**
- `.github/workflows/ci.yml` runs lint and the unit tests on every push.
- The same workflow runs the full loop nightly and on demand, once the repository has
  `ANTHROPIC_API_KEY`, `SHOPIFY_CLIENT_ID` and `SHOPIFY_CLIENT_SECRET` secrets plus
  `SHOPIFY_STORE_DOMAIN` and `SHOPIFY_BUYER_COUNTRY` variables. Results are uploaded as an
  artifact.

## Judge and simulated shopper

Both use `claude-sonnet-5-5` by default (`EVAL_JUDGE_MODEL`, `EVAL_SHOPPER_MODEL`), a
different model from the agent under test (its config's `model`). Current models take no
temperature, so the judge is pinned by model id and effort (`EVAL_JUDGE_EFFORT`, default
`medium`). Each verdict records a fingerprint of the model and the rubric; when either
changes, re-run `python -m evals.audit` and re-baseline. A verdict that fails to parse is
counted as a judge error, separately from agent failures.
