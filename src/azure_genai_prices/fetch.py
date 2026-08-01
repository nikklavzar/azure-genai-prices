"""Fetching the Azure retail price list.

The endpoint is public — no key, no subscription, no auth header — but it is
paginated at 1000 items and the GenAI meters span several product names, so a
full pull is a few dozen requests. Callers are expected to do this on a
schedule and share the result (see `store`), not per price calculation.
"""

from __future__ import annotations

import logging
import time
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


#: A full pull is a few dozen paged requests and Azure rate-limits it, so 429
#: is an expected part of a normal fetch rather than an error. 5xx is retried
#: on the same path; anything else is a real failure and raises immediately.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 2.0


def _retry_delay(response: Any, attempt: int) -> float:
    """Seconds to wait, preferring Azure's own Retry-After when it sends one."""
    retry_after = response.headers.get("Retry-After") if response.headers else None
    if retry_after:
        try:
            return min(float(retry_after), 60.0)
        except (TypeError, ValueError):
            pass
    return min(BACKOFF_BASE_SECONDS * (2**attempt), 60.0)


def _get_json(client: Any, url: str, params: dict | None = None) -> dict:
    last_status = None
    for attempt in range(MAX_RETRIES):
        response = client.get(url, params=params) if params else client.get(url)
        if response.status_code == 200:
            return response.json()

        last_status = response.status_code
        if response.status_code not in RETRY_STATUS:
            break

        delay = _retry_delay(response, attempt)
        logger.warning(
            "azure-genai-prices: %s from the retail price API, retrying in %.1fs (attempt %d/%d)",
            response.status_code,
            delay,
            attempt + 1,
            MAX_RETRIES,
        )
        time.sleep(delay)

    raise RuntimeError(f"Azure retail price API returned {last_status} for {url}")


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
    # Azure returns one row per meter PER REGION, and prices genuinely differ
    # between regions (its data zones are not priced alike). Rows are therefore
    # grouped by (meter, unit, price, effective date) with their regions
    # collected, which keeps the payload small without losing the distinction.
    grouped: dict[tuple, dict] = {}

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
                    group_key = (
                        item.get("productName"),
                        item.get("meterName"),
                        item.get("unitOfMeasure"),
                        item.get("retailPrice"),
                        item.get("effectiveStartDate", ""),
                    )
                    row = grouped.get(group_key)
                    if row is None:
                        row = {
                            "productName": item.get("productName", ""),
                            "meterName": item.get("meterName", ""),
                            "unitOfMeasure": item.get("unitOfMeasure", ""),
                            "retailPrice": item.get("retailPrice", 0),
                            "effectiveStartDate": item.get("effectiveStartDate", ""),
                            "regions": [],
                        }
                        grouped[group_key] = row
                    region = item.get("armRegionName", "") or ""
                    if region not in row["regions"]:
                        row["regions"].append(region)
                url = payload.get("NextPageLink")
                page += 1

        items = list(grouped.values())
        logger.info(
            "azure-genai-prices: fetched %d distinct meter rows across %d products",
            len(items),
            len(products),
        )
        return items
    finally:
        if owns_client:
            client.close()
