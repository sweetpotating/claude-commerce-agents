"""Save the store's catalog to my_store/catalog_snapshot.json, the index the server starts
from when it cannot read the catalog live at boot (e.g. Shopify throttling its IP).

    python -m scripts.catalog_snapshot      # needs SHOPIFY_STORE_DOMAIN; no model calls
"""

from __future__ import annotations

import asyncio
import json

from my_store.shopify_backend import SNAPSHOT_PATH, ShopifyUCPBackend


async def main() -> None:
    backend = ShopifyUCPBackend.from_env()
    index = await backend.catalog_index()
    if not index:
        raise SystemExit("could not read the catalog")
    SNAPSHOT_PATH.write_text(json.dumps(backend.snapshot(), ensure_ascii=False, indent=0) + "\n")
    print(f"{len(index)} products -> {SNAPSHOT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
