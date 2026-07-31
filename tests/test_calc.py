"""Pricing behaviour, against a hand-built catalog with known numbers."""

from __future__ import annotations

from decimal import Decimal

import pytest

from azure_genai_prices import BillingMode
from azure_genai_prices import ContextTier
from azure_genai_prices import DeploymentType
from azure_genai_prices import ModelNotFound
from azure_genai_prices import PriceNotFound
from azure_genai_prices import Usage
from azure_genai_prices import calc_price
from azure_genai_prices import get_meters
from azure_genai_prices import has_long_context_pricing
from azure_genai_prices import list_models
from azure_genai_prices import load_items
from azure_genai_prices import reset


def _row(meter, price, unit="1M"):
    return {
        "productName": "Azure OpenAI GPT5",
        "meterName": meter,
        "unitOfMeasure": unit,
        "retailPrice": price,
        "effectiveStartDate": "2026-07-01T00:00:00Z",
    }


#: Mirrors the real gpt-5.6-luna and gpt-5.4 meter sets.
CATALOG = [
    # luna, short context
    _row("5.6 luna ShortCo Inp Std Gl 1M Tokens", 1.0),
    _row("5.6 luna ShortCo Inp Std DZ 1M Tokens", 1.1),
    _row("5.6 luna ShortCo Cd Inp Std DZ 1M Tokens", 0.11),
    _row("5.6 luna ShortCo Cd Wr Std DZ 1M Tokens", 1.375),
    _row("5.6 luna ShortCo Opt Std DZ 1M Tokens", 6.6),
    # luna, long context
    _row("5.6 luna LongCo Inp Std DZ 1M Tokens", 2.2),
    _row("5.6 luna LongCo Cd Inp Std DZ 1M Tokens", 0.22),
    _row("5.6 luna LongCo Cd Wr Std DZ 1M Tokens", 2.75),
    _row("5.6 luna LongCo Opt Std DZ 1M Tokens", 9.9),
    # luna, batch + priority
    _row("5.6 luna ShortCo Inp PP DZ 1M Tokens", 2.2),
    _row("5.6 luna ShortCo Opt PP DZ 1M Tokens", 13.2),
    # a mini tier with no long-context meter at all
    _row("5.4 mini Inp Dz 1M Tokens", 0.9),
    _row("5.4 mini Opt Dz 1M Tokens", 5.4),
]


@pytest.fixture(autouse=True)
def _catalog():
    load_items(CATALOG, "test")
    yield
    reset()


class TestDeploymentPricing:
    def test_data_zone_costs_more_than_global(self):
        # Pinned to SHORT so this compares tiers, not context tiers — 1M input
        # tokens would otherwise cross the long-context threshold.
        usage = Usage(input_tokens=1_000_000)
        glb = calc_price(usage, "gpt-5.6-luna", DeploymentType.GLOBAL, context=ContextTier.SHORT)
        dz = calc_price(usage, "gpt-5.6-luna", DeploymentType.DATA_ZONE, context=ContextTier.SHORT)
        assert glb.input_cost == Decimal("1.0")
        assert dz.input_cost == Decimal("1.1")

    def test_global_is_the_default(self):
        price = calc_price(Usage(input_tokens=1_000_000), "gpt-5.6-luna", context=ContextTier.SHORT)
        assert price.input_cost == Decimal("1.0")

    def test_deployment_accepts_a_plain_string(self):
        price = calc_price(
            Usage(input_tokens=1_000_000),
            "gpt-5.6-luna",
            "data_zone",
            context=ContextTier.SHORT,
        )
        assert price.input_cost == Decimal("1.1")


class TestLongContext:
    """Azure bills a request that crosses the threshold WHOLLY on the long
    meters — output included. A per-token split would understate it."""

    def test_below_threshold_uses_short_meters(self):
        price = calc_price(
            Usage(input_tokens=200_000, output_tokens=1_000),
            "gpt-5.6-luna",
            DeploymentType.DATA_ZONE,
        )
        assert price.context is ContextTier.SHORT
        assert price.input_cost == Decimal("0.22")
        assert price.output_cost == Decimal("0.0066")

    def test_above_threshold_switches_input_and_output(self):
        price = calc_price(
            Usage(input_tokens=300_000, output_tokens=1_000),
            "gpt-5.6-luna",
            DeploymentType.DATA_ZONE,
        )
        assert price.context is ContextTier.LONG
        assert price.input_cost == Decimal("0.66")
        # The output rate switched too: 1000 * $9.90/M, not $6.60/M.
        assert price.output_cost == Decimal("0.0099")

    def test_threshold_is_exclusive(self):
        exactly = calc_price(Usage(input_tokens=272_000), "gpt-5.6-luna", DeploymentType.DATA_ZONE)
        assert exactly.context is ContextTier.SHORT

    def test_threshold_is_configurable(self):
        price = calc_price(
            Usage(input_tokens=100_000),
            "gpt-5.6-luna",
            DeploymentType.DATA_ZONE,
            long_context_threshold=50_000,
        )
        assert price.context is ContextTier.LONG

    def test_context_can_be_forced(self):
        price = calc_price(
            Usage(input_tokens=1_000),
            "gpt-5.6-luna",
            DeploymentType.DATA_ZONE,
            context=ContextTier.LONG,
        )
        assert price.input_cost == Decimal("0.0022")

    def test_model_without_long_meters_falls_back(self):
        """gpt-5.4-mini has no LongCo meter — a huge prompt still bills at its
        single rate rather than failing to price."""
        price = calc_price(Usage(input_tokens=1_000_000), "gpt-5.4-mini", DeploymentType.DATA_ZONE)
        assert price.context is ContextTier.LONG  # it did cross the threshold
        assert price.input_cost == Decimal("0.9")  # ...but bills the only rate there is

    def test_has_long_context_pricing(self):
        assert has_long_context_pricing("gpt-5.6-luna", DeploymentType.DATA_ZONE)
        assert not has_long_context_pricing("gpt-5.4-mini", DeploymentType.DATA_ZONE)


class TestCaching:
    def test_cached_tokens_bill_at_the_cached_rate(self):
        price = calc_price(
            Usage(input_tokens=1_000_000, cache_read_tokens=800_000),
            "gpt-5.6-luna",
            DeploymentType.DATA_ZONE,
            context=ContextTier.SHORT,
        )
        # 200k uncached @ $1.10/M + 800k cached @ $0.11/M
        assert price.input_cost == Decimal("0.308")

    def test_cached_tokens_are_a_subset_of_input(self):
        """Both providers report cached tokens inside input_tokens. Counting
        them twice would inflate every cached request."""
        usage = Usage(input_tokens=1_000_000, cache_read_tokens=1_000_000)
        price = calc_price(
            usage, "gpt-5.6-luna", DeploymentType.DATA_ZONE, context=ContextTier.SHORT
        )
        assert price.input_cost == Decimal("0.11")

    def test_cache_reads_exceeding_input_are_clamped(self):
        usage = Usage(input_tokens=1000, cache_read_tokens=5000)
        assert usage.cache_read_tokens == 1000

    def test_cache_writes_bill_on_their_own_meter(self):
        price = calc_price(
            Usage(input_tokens=0, cache_write_tokens=1_000_000),
            "gpt-5.6-luna",
            DeploymentType.DATA_ZONE,
        )
        assert price.input_cost == Decimal("1.375")

    def test_cache_writes_are_free_when_unmetered(self):
        """Only gpt-5.6+ meters writes. On a model that does not, a write must
        not be charged at all — inventing a rate would overstate the bill."""
        price = calc_price(
            Usage(input_tokens=0, cache_write_tokens=1_000_000),
            "gpt-5.4-mini",
            DeploymentType.DATA_ZONE,
        )
        assert price.input_cost == Decimal("0")

    def test_missing_cache_read_meter_falls_back_to_input_rate(self):
        price = calc_price(
            Usage(input_tokens=1_000_000, cache_read_tokens=1_000_000),
            "gpt-5.4-mini",
            DeploymentType.DATA_ZONE,
        )
        assert price.input_cost == Decimal("0.9")  # no cached meter -> input rate


class TestBillingModes:
    def test_priority_costs_more(self):
        usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
        std = calc_price(usage, "gpt-5.6-luna", DeploymentType.DATA_ZONE, context=ContextTier.SHORT)
        pp = calc_price(
            usage,
            "gpt-5.6-luna",
            DeploymentType.DATA_ZONE,
            mode=BillingMode.PRIORITY,
            context=ContextTier.SHORT,
        )
        assert std.total_cost == Decimal("7.7")
        assert pp.total_cost == Decimal("15.4")

    def test_unavailable_mode_raises_rather_than_guessing(self):
        with pytest.raises(PriceNotFound):
            calc_price(
                Usage(input_tokens=1000),
                "gpt-5.6-luna",
                DeploymentType.DATA_ZONE,
                mode=BillingMode.BATCH,
            )


class TestErrorsAndIntrospection:
    def test_unknown_model_raises(self):
        with pytest.raises(ModelNotFound):
            calc_price(Usage(input_tokens=1), "gpt-does-not-exist")

    def test_unknown_deployment_raises(self):
        with pytest.raises(PriceNotFound):
            calc_price(Usage(input_tokens=1000), "gpt-5.4-mini", DeploymentType.REGIONAL)

    def test_zero_usage_is_free_not_an_error(self):
        assert calc_price(Usage(), "gpt-5.6-luna").total_cost == Decimal("0")

    def test_meters_used_is_auditable(self):
        price = calc_price(
            Usage(input_tokens=1000, output_tokens=10),
            "gpt-5.6-luna",
            DeploymentType.DATA_ZONE,
        )
        assert price.meters_used == [
            "5.6 luna ShortCo Inp Std DZ 1M Tokens",
            "5.6 luna ShortCo Opt Std DZ 1M Tokens",
        ]

    def test_total_is_input_plus_output(self):
        price = calc_price(
            Usage(input_tokens=1_000_000, output_tokens=1_000_000),
            "gpt-5.6-luna",
            DeploymentType.DATA_ZONE,
            context=ContextTier.SHORT,
        )
        assert price.total_cost == price.input_cost + price.output_cost == Decimal("7.7")

    def test_model_lookup_is_case_insensitive(self):
        assert calc_price(Usage(input_tokens=1000), "GPT-5.6-Luna").model == "gpt-5.6-luna"

    def test_list_models_and_get_meters(self):
        assert "gpt-5.6-luna" in list_models()
        assert len(get_meters("gpt-5.4-mini")) == 2
