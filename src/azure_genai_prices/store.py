"""Where the price snapshot lives, and how it gets refreshed.

Three layers, in the order they are consulted:

1. The in-process table — what `calc_price` reads. Never hits the network.
2. Redis — the shared snapshot. One scheduled job calls
   `fetch_and_store_prices`; every process calls `load_prices_from_redis` at
   startup and gets the same data without its own API pull.
3. The snapshot bundled in the wheel — so a fresh install prices correctly
   offline, with no Redis and no network, from the day it was built.

This mirrors how `genai-prices` is deployed in production, deliberately: it is
a pattern that survives a provider outage, because a stale price is a far
better failure than no price.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from importlib import resources
from typing import Any

from .meters import DEFAULT_PRODUCTS
from .meters import Meter
from .meters import ParseReport
from .meters import parse_catalog

logger = logging.getLogger(__name__)

REDIS_DATA_KEY = "azure_genai_prices:data"
REDIS_TIMESTAMP_KEY = "azure_genai_prices:updated_at"

_BUNDLED_SNAPSHOT = "snapshot.json"

_lock = threading.RLock()
_table: dict[tuple, Meter] | None = None
_report: ParseReport | None = None
_source: str = "none"


def _apply_items(items: list[dict], source: str) -> ParseReport:
    global _table, _report, _source
    table, report = parse_catalog(items)
    with _lock:
        _table = table
        _report = report
        _source = source
    if report.failed:
        logger.warning(
            "azure-genai-prices: %d meter(s) could not be classified and are "
            "not priced (e.g. %s). Open an issue with these names.",
            len(report.failed),
            ", ".join(sorted(report.failed)[:3]),
        )
    logger.info("azure-genai-prices: loaded %d meters from %s", report.parsed, source)
    return report


def load_bundled_snapshot() -> ParseReport:
    """Load the snapshot shipped inside the package."""
    raw = (
        resources.files("azure_genai_prices.data")
        .joinpath(_BUNDLED_SNAPSHOT)
        .read_text(encoding="utf-8")
    )
    payload = json.loads(raw)
    return _apply_items(payload["items"], f"bundled snapshot ({payload.get('fetched_at', '?')})")


def refresh_from_azure(
    products: tuple[str, ...] = DEFAULT_PRODUCTS,
    currency: str = "USD",
) -> ParseReport:
    """Fetch straight from Azure into this process. No Redis involved."""
    from .fetch import fetch_price_items

    items = fetch_price_items(products=products, currency=currency)
    return _apply_items(items, "azure retail api")


def _redis_client(redis_url: str) -> Any:
    try:
        import redis
    except ImportError as exc:  # pragma: no cover - optional extra
        raise RuntimeError(
            "redis is required for the Redis-backed store. Install with: "
            "pip install azure-genai-prices[redis]"
        ) from exc
    return redis.from_url(redis_url)


def fetch_and_store_prices(
    redis_url: str,
    products: tuple[str, ...] = DEFAULT_PRODUCTS,
    currency: str = "USD",
) -> bool:
    """Fetch from Azure, cache in Redis, and apply to this process.

    This is the scheduled-job entry point. Returns True on success; on failure
    it logs and returns False rather than raising, so a price refresh can
    never take down the caller's worker.
    """
    try:
        from .fetch import fetch_price_items

        items = fetch_price_items(products=products, currency=currency)
        payload = json.dumps({"fetched_at": datetime.now().isoformat(), "items": items})

        client = _redis_client(redis_url)
        try:
            client.set(REDIS_DATA_KEY, payload)
            client.set(REDIS_TIMESTAMP_KEY, datetime.now().isoformat())
        finally:
            client.close()

        _apply_items(items, "azure retail api (stored to redis)")
        return True
    except Exception:
        logger.exception("azure-genai-prices: failed to fetch/store prices")
        return False


def load_prices_from_redis(redis_url: str, fallback: bool = True) -> bool:
    """Load the shared snapshot from Redis into this process.

    Returns True if Redis supplied the data. When it did not and ``fallback``
    is set, the bundled snapshot is loaded instead so the caller still has
    usable prices — stale, but present.
    """
    try:
        client = _redis_client(redis_url)
        try:
            raw = client.get(REDIS_DATA_KEY)
        finally:
            client.close()

        if raw:
            payload = json.loads(raw)
            _apply_items(payload["items"], "redis")
            return True
        logger.info("azure-genai-prices: no snapshot in Redis at %s", REDIS_DATA_KEY)
    except Exception:
        logger.exception("azure-genai-prices: failed to load prices from Redis")

    if fallback and not is_loaded():
        load_bundled_snapshot()
    return False


def snapshot_updated_at(redis_url: str) -> str | None:
    """When the Redis snapshot was last written, if it exists."""
    try:
        client = _redis_client(redis_url)
        try:
            value = client.get(REDIS_TIMESTAMP_KEY)
        finally:
            client.close()
        return value.decode() if isinstance(value, bytes) else value
    except Exception:
        logger.exception("azure-genai-prices: failed to read snapshot timestamp")
        return None


def load_items(items: list[dict], source: str = "caller") -> ParseReport:
    """Apply a raw Azure price list supplied by the caller. Mostly for tests."""
    return _apply_items(items, source)


def is_loaded() -> bool:
    with _lock:
        return _table is not None


def get_table() -> dict[tuple, Meter]:
    """The active meter table, loading the bundled snapshot on first use."""
    with _lock:
        if _table is not None:
            return _table
    load_bundled_snapshot()
    with _lock:
        assert _table is not None
        return _table


def get_report() -> ParseReport | None:
    with _lock:
        return _report


def get_source() -> str:
    with _lock:
        return _source


def reset() -> None:
    """Drop the loaded table. Mostly for tests."""
    global _table, _report, _source
    with _lock:
        _table = None
        _report = None
        _source = "none"
