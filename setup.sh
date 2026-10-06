#!/usr/bin/env bash
# Clone the reference packages into vendor/ and install them plus this starter kit's deps.
set -euo pipefail
cd "$(dirname "$0")"
[ -d vendor/commerce-agents ] || git clone --depth 1 https://github.com/anthropics/commerce-agents vendor/commerce-agents
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
(cd vendor/commerce-agents && pip install -q -r requirements.txt)  # its paths are relative
echo "Ready. Run: source .venv/bin/activate && python -m scripts.walkthrough"
