"""The suite runs offline on the sample store. my_store.app picks its backend from
STORE_BACKEND at import, and a Shopify environment sets it to "shopify"; pin it here,
before any test imports the app. Shopify itself is tested against fake_shopify.py."""

import os

os.environ["STORE_BACKEND"] = "sample"
