"""Fetching the Azure retail price list.

The endpoint is public — no key, no subscription, no auth header — but it is
paginated at 1000 items and the GenAI meters span several product names, so a
full pull is a few dozen requests. Callers are expected to do this on a
schedule and share the result (see `store`), not per price calculation.
"""

from __future__ import annotations

import logging
from typing import Any

from .meters import DEFAULT_PRODUCTS

logger = logging.getLogger(__name__)

RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"
API_VERSION = "2023-01-01-preview"
DEFAULT_TIMEOUT = 30.0

#: Azure rejects an over-long $filter, and OR-ing every product into one query
#: is slower than issuing one query per product, so they are fetched
#: separately and merged.
_FILTER_TEMPLATE = "contains(productName, '{product}')"


def _get_json(client: Any, url: str, params: dict | None = None) -> dict:
    response = client.get(url, params=params) if params else client.get(url)
    if response.status_code != 200:
        raise RuntimeError(f"Azure retail price API returned {response.status_code} for {url}")
    return response.json()


def fetch_price_items(
    products: tuple[str, ...] = DEFAULT_PRODUCTS,
    currency: str = "USD",
    timeout: float = DEFAULT_TIMEOUT,
    client: Any | None = None,
) -> list[dict]:
    """Pull every retail price row for the given Azure products.

    Pass ``client`` to reuse an existing ``httpx.Client`` (or anything with a
    compatible ``.get``); otherwise one is created and closed here.
    """
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RuntimeError(
            "httpx is required to fetch prices. Install azure-genai-prices "
            "with its default dependencies."
        ) from exc

    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    items: list[dict] = []
    # Azure repeats a meter per region; dedupe as we go so the payload we cache
    # stays small (~20k rows collapse to a few hundred distinct meters).
    seen: set[tuple] = set()

    try:
        for product in products:
            params = {
                "api-version": API_VERSION,
                "$filter": _FILTER_TEMPLATE.format(product=product),
                "currencyCode": currency,
            }
            url: str | None = RETAIL_PRICES_URL
            page = 0
            while url:
                payload = _get_json(client, url, params if page == 0 else None)
                for item in payload.get("Items", []):
                    dedupe_key = (
                        item.get("productName"),
                        item.get("meterName"),
                        item.get("unitOfMeasure"),
                        item.get("retailPrice"),
                    )
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    items.append(
                        {
                            "productName": item.get("productName", ""),
                            "meterName": item.get("meterName", ""),
                            "unitOfMeasure": item.get("unitOfMeasure", ""),
                            "retailPrice": item.get("retailPrice", 0),
                            "effectiveStartDate": item.get("effectiveStartDate", ""),
                        }
                    )
                url = payload.get("NextPageLink")
                page += 1
        logger.info(
            "azure-genai-prices: fetched %d distinct meter rows across %d products",
            len(items),
            len(products),
        )
        return items
    finally:
        if owns_client:
            client.close()
