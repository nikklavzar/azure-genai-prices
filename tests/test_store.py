"""Snapshot loading, Redis sync, and the bundled fallback."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from azure_genai_prices import DeploymentType
from azure_genai_prices import Usage
from azure_genai_prices import calc_price
from azure_genai_prices import get_report
from azure_genai_prices import get_source
from azure_genai_prices import is_loaded
from azure_genai_prices import list_models
from azure_genai_prices import load_bundled_snapshot
from azure_genai_prices import reset
from azure_genai_prices import store


class FakeRedis:
    """Minimal stand-in — the real client is an optional extra."""

    def __init__(self, data=None, fail=False):
        self.data = dict(data or {})
        self.fail = fail
        self.closed = False

    def get(self, key):
        if self.fail:
            raise ConnectionError("redis is down")
        return self.data.get(key)

    def set(self, key, value):
        if self.fail:
            raise ConnectionError("redis is down")
        self.data[key] = value

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _clean():
    reset()
    yield
    reset()


@pytest.fixture
def catalog():
    return [
        {
            "productName": "Azure OpenAI GPT5",
            "meterName": "5.6 luna ShortCo Inp Std DZ 1M Tokens",
            "unitOfMeasure": "1M",
            "retailPrice": 1.1,
            "effectiveStartDate": "2026-07-01T00:00:00Z",
        }
    ]


class TestBundledSnapshot:
    """A fresh install must price correctly with no Redis and no network."""

    def test_bundled_snapshot_loads(self):
        report = load_bundled_snapshot()
        assert report.parsed > 100
        assert is_loaded()

    def test_bundled_snapshot_has_no_unparsed_meters(self):
        """If Azure ships a naming style we cannot read, this fails at build
        time rather than silently pricing that model at $0."""
        load_bundled_snapshot()
        report = get_report()
        assert report.failed == [], report.failed

    def test_bundled_snapshot_covers_the_gpt5_family(self):
        load_bundled_snapshot()
        models = list_models()
        for expected in ("gpt-5-mini", "gpt-5.1", "gpt-5.4", "gpt-5.6-luna"):
            assert expected in models

    def test_table_autoloads_on_first_use(self):
        assert not is_loaded()
        price = calc_price(Usage(input_tokens=1_000_000), "gpt-5.6-luna", DeploymentType.DATA_ZONE)
        assert price.input_cost > 0
        assert is_loaded()


class TestRedisSync:
    def test_fetch_and_store_writes_both_keys(self, monkeypatch, catalog):
        fake = FakeRedis()
        monkeypatch.setattr(store, "_redis_client", lambda url: fake)
        monkeypatch.setattr("azure_genai_prices.fetch.fetch_price_items", lambda **kw: catalog)

        assert store.fetch_and_store_prices("redis://x") is True
        assert store.REDIS_DATA_KEY in fake.data
        assert store.REDIS_TIMESTAMP_KEY in fake.data
        assert fake.closed
        # And it applied to this process, so no second fetch is needed.
        assert get_source().startswith("azure retail api")

    def test_load_from_redis_uses_the_shared_snapshot(self, monkeypatch, catalog):
        payload = json.dumps({"fetched_at": "2026-07-31T00:00:00", "items": catalog})
        fake = FakeRedis({store.REDIS_DATA_KEY: payload})
        monkeypatch.setattr(store, "_redis_client", lambda url: fake)

        assert store.load_prices_from_redis("redis://x") is True
        assert get_source() == "redis"
        price = calc_price(Usage(input_tokens=1_000_000), "gpt-5.6-luna", DeploymentType.DATA_ZONE)
        assert price.input_cost == Decimal("1.1")

    def test_missing_snapshot_falls_back_to_bundled(self, monkeypatch):
        monkeypatch.setattr(store, "_redis_client", lambda url: FakeRedis())
        assert store.load_prices_from_redis("redis://x") is False
        assert is_loaded()
        assert "bundled" in get_source()

    def test_redis_outage_falls_back_rather_than_raising(self, monkeypatch):
        """A price refresh must never take down the caller. A stale price is a
        far better failure than an exception in a billing path."""
        monkeypatch.setattr(store, "_redis_client", lambda url: FakeRedis(fail=True))
        assert store.load_prices_from_redis("redis://x") is False
        assert is_loaded()

    def test_fetch_failure_returns_false_not_raises(self, monkeypatch):
        def boom(**kwargs):
            raise RuntimeError("azure is down")

        monkeypatch.setattr("azure_genai_prices.fetch.fetch_price_items", boom)
        monkeypatch.setattr(store, "_redis_client", lambda url: FakeRedis())
        assert store.fetch_and_store_prices("redis://x") is False

    def test_fallback_does_not_clobber_a_loaded_table(self, monkeypatch, catalog):
        """A later Redis miss must not replace good in-memory prices with the
        older bundled ones."""
        store.load_items(catalog, "test")
        monkeypatch.setattr(store, "_redis_client", lambda url: FakeRedis())
        store.load_prices_from_redis("redis://x")
        assert get_source() == "test"

    def test_snapshot_timestamp(self, monkeypatch):
        fake = FakeRedis({store.REDIS_TIMESTAMP_KEY: b"2026-07-31T12:00:00"})
        monkeypatch.setattr(store, "_redis_client", lambda url: fake)
        assert store.snapshot_updated_at("redis://x") == "2026-07-31T12:00:00"
