#!/usr/bin/env bash
# Render build step: fetch the reference agent at the commit this kit was tested against,
# then install it. Move VENDOR_COMMIT deliberately, after running the tests on the new one.
set -euo pipefail
VENDOR_COMMIT="${VENDOR_COMMIT:-fd4d59224ab96b43c6dc6888207c67b3bd5a24cf}"
rm -rf vendor/commerce-agents
mkdir -p vendor/commerce-agents
cd vendor/commerce-agents
git init -q
git remote add origin https://github.com/anthropics/commerce-agents
git fetch -q --depth 1 origin "$VENDOR_COMMIT"
git checkout -q FETCH_HEAD
pip install -r requirements.txt
