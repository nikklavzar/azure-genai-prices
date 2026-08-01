"""Fetching: Azure rate-limits a full pull, so retrying is normal operation."""

from __future__ import annotations

import pytest

from azure_genai_prices import fetch
from azure_genai_prices.fetch import fetch_price_items


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {"Items": []}
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeClient:
    """Returns each queued response in turn, then empty pages forever."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, params=None):
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(200, {"Items": []})

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(fetch.time, "sleep", lambda _s: None)


ROW = {
    "productName": "Azure OpenAI GPT5",
    "meterName": "5.6 luna ShortCo Inp Std DZ 1M Tokens",
    "unitOfMeasure": "1M",
    "retailPrice": 1.1,
    "effectiveStartDate": "2026-07-01T00:00:00Z",
    "armRegionName": "swedencentral",
}


def test_rate_limit_is_retried_not_raised():
    """A 429 mid-pull used to abort the whole refresh — which, on a schedule,
    means prices silently stop updating."""
    client = FakeClient([FakeResponse(429), FakeResponse(429), FakeResponse(200, {"Items": [ROW]})])
    items = fetch_price_items(products=("Azure OpenAI",), client=client)
    assert len(items) == 1
    assert client.calls == 3


def test_server_errors_are_retried():
    client = FakeClient([FakeResponse(503), FakeResponse(200, {"Items": [ROW]})])
    assert len(fetch_price_items(products=("Azure OpenAI",), client=client)) == 1


def test_client_errors_are_not_retried():
    """A 400 will not fix itself; retrying just delays the failure."""
    client = FakeClient([FakeResponse(400)])
    with pytest.raises(RuntimeError, match="400"):
        fetch_price_items(products=("Azure OpenAI",), client=client)
    assert client.calls == 1


def test_retry_eventually_gives_up():
    client = FakeClient([FakeResponse(429) for _ in range(fetch.MAX_RETRIES + 2)])
    with pytest.raises(RuntimeError, match="429"):
        fetch_price_items(products=("Azure OpenAI",), client=client)
    assert client.calls == fetch.MAX_RETRIES


def test_retry_after_header_is_honoured(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(fetch.time, "sleep", slept.append)
    client = FakeClient([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200)])
    fetch_price_items(products=("Azure OpenAI",), client=client)
    assert slept == [7.0]


def test_absurd_retry_after_is_capped():
    assert fetch._retry_delay(FakeResponse(429, headers={"Retry-After": "9999"}), 0) == 60.0


def test_garbage_retry_after_falls_back_to_backoff():
    r = FakeResponse(429, headers={"Retry-After": "soon"})
    assert fetch._retry_delay(r, 0) == fetch.BACKOFF_BASE_SECONDS


def test_regions_are_collected_per_price():
    """Rows differing only by region collapse, keeping every region."""
    other = dict(ROW, armRegionName="francecentral")
    client = FakeClient([FakeResponse(200, {"Items": [ROW, other]})])
    items = fetch_price_items(products=("Azure OpenAI",), client=client)
    assert items[0]["regions"] == ["swedencentral", "francecentral"]
