#!/usr/bin/env bash
# The test-and-fix loop: every check a change must pass, cheapest first. Stops at the first
# failing stage; the eval stage writes evals/results/latest.md (failures listed with their
# reasons, and the diff against evals/baseline.json).
#
#   scripts/loop.sh                    # lint, unit tests, live smoke, full evals
#   scripts/loop.sh --tags conversion  # extra args go to the eval runner
#   OFFLINE=1 scripts/loop.sh          # lint and unit tests only (no keys, no network)
#   NO_MODEL=1 scripts/loop.sh         # everything but the evals: no Anthropic API call
#
# Fixing loop: read latest.md -> fix the code, prompt, or case (say which in the commit) ->
# re-run the failing ids (python -m evals.runner --ids <id> -n 3) -> run this script ->
# when it is clean and better than the baseline, promote it: --update-baseline.
set -uo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
[ -f .venv/bin/activate ] && source .venv/bin/activate

stage() { printf '\n== %s\n' "$1"; }

stage "lint"
ruff check . && ruff format --check . || exit 1

stage "unit tests (offline: fake Shopify store, sample catalog)"
python -m pytest -q || exit 1

if [ -n "${OFFLINE:-}" ]; then
  echo "OFFLINE set: skipping the live stages"; exit 0
fi
if [ -z "${SHOPIFY_STORE_DOMAIN:-}" ]; then
  echo "SHOPIFY_STORE_DOMAIN is not set: skipping the live stages"; exit 0
fi

stage "live smoke (the store's UCP tools, no model)"
python -m scripts.shopify_smoke --query tee | grep -E '^\[(PASS|FAIL|SKIP)\]' || exit 1

stage "end to end, scripted model (every key flow through the app and live store, no API)"
python -m e2e.run_api | tail -1
[ "${PIPESTATUS[0]}" -eq 0 ] || exit 1

if command -v node >/dev/null && [ -x /opt/pw-browsers/chromium-1194/chrome-linux/chrome ]; then
  stage "shopper walk in Chromium at phone width (scripted model)"
  python -m scripts.e2e_server >/tmp/e2e_server.log 2>&1 &
  server=$!
  trap 'kill $server 2>/dev/null' EXIT
  for _ in $(seq 30); do curl -sf -o /dev/null localhost:8000/ && break; sleep 1; done
  node e2e/shopper.js "$(npm root -g)" docs/uat/e2e | grep -E 'FAIL|checks passed'
  [ "${PIPESTATUS[0]}" -eq 0 ] || exit 1
  kill $server 2>/dev/null
fi

if [ -n "${NO_MODEL:-}" ] || [ -z "${ANTHROPIC_API_KEY:-${STORE_AGENT_ANTHROPIC_API_KEY:-}}" ]; then
  echo "NO_MODEL set or no Anthropic key: skipping the evals"; exit 0
fi

stage "evals (live store, real model, judged)"
python -m evals.runner "$@"
