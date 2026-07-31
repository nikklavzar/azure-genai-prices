"""Bugs found in adversarial review. Each one was live and silent.

Every failure here is the same shape: the library returned a number that
looked plausible and was wrong. Those are worse than an exception, because
nothing downstream ever questions a cost report.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from azure_genai_prices import AmbiguousRegionPrice
from azure_genai_prices import DeploymentType
from azure_genai_prices import PriceNotFound
from azure_genai_prices import Usage
from azure_genai_prices import calc_price
from azure_genai_prices import get_meters
from azure_genai_prices import list_regions
from azure_genai_prices import load_items
from azure_genai_prices import reset


def _row(meter, price, regions, effective="2026-06-01", unit="1M"):
    return {
        "productName": "Azure OpenAI GPT5",
        "meterName": meter,
        "unitOfMeasure": unit,
        "retailPrice": price,
        "effectiveStartDate": f"{effective}T00:00:00Z",
        "regions": regions,
    }


#: The real shape of the gpt-5.4-mini data-zone meters: EU/US and APAC are
#: priced 9% apart on the same meter, same effective date.
CATALOG = [
    _row("5.4 mini Inp Dz 1M Tokens", 0.825, ["northeurope", "eastus"]),
    _row("5.4 mini Inp Dz 1M Tokens", 0.9, ["japaneast", "australiaeast"]),
    _row("5.4 mini Opt Dz 1M Tokens", 4.95, ["northeurope", "eastus"]),
    _row("5.4 mini Opt Dz 1M Tokens", 5.4, ["japaneast", "australiaeast"]),
    _row("5.6 luna ShortCo Inp Std DZ 1M Tokens", 1.1, ["northeurope", "japaneast"]),
    _row("5.6 luna ShortCo Opt Std DZ 1M Tokens", 6.6, ["northeurope", "japaneast"]),
]


@pytest.fixture(autouse=True)
def _catalog():
    load_items(CATALOG, "test")
    yield
    reset()


class TestRegionDependentPricing:
    """Azure runs several data zones and does not price them alike, so "the
    Data Zone price" is not one number. Collapsing them silently picked
    whichever row the API happened to return first."""

    def test_ambiguous_price_raises_instead_of_guessing(self):
        with pytest.raises(AmbiguousRegionPrice) as exc:
            calc_price(Usage(input_tokens=1000), "gpt-5.4-mini", DeploymentType.DATA_ZONE)
        assert "region" in str(exc.value)

    def test_region_selects_the_right_data_zone(self):
        eu = calc_price(
            Usage(input_tokens=1_000_000),
            "gpt-5.4-mini",
            DeploymentType.DATA_ZONE,
            region="northeurope",
        )
        apac = calc_price(
            Usage(input_tokens=1_000_000),
            "gpt-5.4-mini",
            DeploymentType.DATA_ZONE,
            region="japaneast",
        )
        assert eu.input_cost == Decimal("0.825")
        assert apac.input_cost == Decimal("0.9")

    def test_region_is_recorded_on_the_result(self):
        price = calc_price(
            Usage(input_tokens=1000), "gpt-5.4-mini", DeploymentType.DATA_ZONE, region="eastus"
        )
        assert price.region == "eastus"

    def test_unambiguous_meter_needs_no_region(self):
        """Most meters are priced alike everywhere; requiring a region for
        those would be noise."""
        price = calc_price(Usage(input_tokens=1_000_000), "gpt-5.6-luna", DeploymentType.DATA_ZONE)
        assert price.input_cost == Decimal("1.1")

    def test_region_lookup_is_case_insensitive(self):
        price = calc_price(
            Usage(input_tokens=1_000_000),
            "gpt-5.4-mini",
            DeploymentType.DATA_ZONE,
            region="NorthEurope",
        )
        assert price.input_cost == Decimal("0.825")

    def test_unknown_region_raises_when_the_price_is_ambiguous(self):
        with pytest.raises(PriceNotFound):
            calc_price(
                Usage(input_tokens=1000),
                "gpt-5.4-mini",
                DeploymentType.DATA_ZONE,
                region="mars-central-1",
            )

    def test_unknown_region_is_tolerated_when_the_price_is_not_in_doubt(self):
        """A region disambiguates; it does not restrict. Azure's published
        region list lags real availability, so refusing to price a call whose
        answer is identical everywhere would be false precision."""
        price = calc_price(
            Usage(input_tokens=1_000_000),
            "gpt-5.6-luna",
            DeploymentType.DATA_ZONE,
            region="a-region-azure-has-not-listed",
        )
        assert price.input_cost == Decimal("1.1")

    def test_meter_exposes_its_regions(self):
        meter = next(m for m in get_meters("gpt-5.4-mini") if m.key.kind.value == "input")
        assert meter.is_region_dependent
        assert set(meter.regions) == {"northeurope", "eastus", "japaneast", "australiaeast"}

    def test_list_regions(self):
        assert "japaneast" in list_regions("gpt-5.4-mini")


class TestNoSilentZeros:
    """A $0 cost for a real call is the failure mode this library exists to
    prevent, so every path that cannot find a rate must raise."""

    def test_cached_only_usage_on_a_missing_deployment_raises(self):
        with pytest.raises(PriceNotFound):
            calc_price(
                Usage(input_tokens=1000, cache_read_tokens=1000),
                "gpt-5.6-luna",
                DeploymentType.REGIONAL,
            )

    def test_cache_write_only_usage_on_a_missing_deployment_raises(self):
        with pytest.raises(PriceNotFound):
            calc_price(
                Usage(cache_write_tokens=1_000_000),
                "gpt-5.6-luna",
                DeploymentType.REGIONAL,
            )

    def test_cache_write_stays_free_when_the_model_simply_does_not_meter_it(self):
        """The guard above must not start charging for writes on models where
        Azure genuinely folds them into the input rate."""
        price = calc_price(
            Usage(cache_write_tokens=1_000_000),
            "gpt-5.4-mini",
            DeploymentType.DATA_ZONE,
            region="northeurope",
        )
        assert price.input_cost == Decimal("0")


class TestNegativeUsage:
    """A negative cost silently cancels real spend out of an aggregate."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"input_tokens": -1},
            {"output_tokens": -1},
            {"cache_read_tokens": -1},
            {"cache_write_tokens": -1},
            {"search_units": -1},
        ],
    )
    def test_negative_counts_are_rejected(self, kwargs):
        with pytest.raises(ValueError):
            Usage(**kwargs)


class TestPerRegionSupersede:
    """Superseded rows must be resolved per region: a 2026-03 row for the EU
    must not lose to a 2026-06 row that only applies to APAC."""

    def test_each_region_keeps_its_own_current_price(self):
        catalog = [
            _row("5.4 mini Inp Dz 1M Tokens", 0.825, ["northeurope"], effective="2026-03-01"),
            _row("5.4 mini Inp Dz 1M Tokens", 0.9, ["japaneast"], effective="2026-06-01"),
        ]
        load_items(catalog, "test")
        eu = calc_price(
            Usage(input_tokens=1_000_000),
            "gpt-5.4-mini",
            DeploymentType.DATA_ZONE,
            region="northeurope",
        )
        assert eu.input_cost == Decimal("0.825")

    def test_a_repricing_in_one_region_wins_only_there(self):
        catalog = [
            _row(
                "5.4 mini Inp Dz 1M Tokens",
                0.825,
                ["northeurope", "japaneast"],
                effective="2026-03-01",
            ),
            _row("5.4 mini Inp Dz 1M Tokens", 0.9, ["japaneast"], effective="2026-06-01"),
        ]
        load_items(catalog, "test")
        meter = next(m for m in get_meters("gpt-5.4-mini") if m.key.kind.value == "input")
        assert meter.price_for("northeurope") * 1_000_000 == Decimal("0.825")
        assert meter.price_for("japaneast") * 1_000_000 == Decimal("0.9")
