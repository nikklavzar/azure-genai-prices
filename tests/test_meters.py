"""Meter-name parsing: the part that rots if Azure changes its naming."""

from __future__ import annotations

from decimal import Decimal

import pytest

from azure_genai_prices import BillingMode
from azure_genai_prices import ContextTier
from azure_genai_prices import DeploymentType
from azure_genai_prices import PriceKind
from azure_genai_prices.meters import parse_catalog
from azure_genai_prices.meters import parse_meter_name


def _row(meter, price, unit="1M", product="Azure OpenAI GPT5", effective="2026-01-01"):
    return {
        "productName": product,
        "meterName": meter,
        "unitOfMeasure": unit,
        "retailPrice": price,
        "effectiveStartDate": f"{effective}T00:00:00Z",
    }


class TestDeploymentTier:
    """The whole point of the library: Data Zone must never be read as Global."""

    @pytest.mark.parametrize(
        "meter",
        [
            "GPT 5 Mini Inpt DZone 1M Tokens",
            "GPT 5.1 inp Dz 1M Tokens",
            "5.4 mini Inp Dz 1M Tokens",
            "5.6 luna ShortCo Inp Std DZ 1M Tokens",
            "o3 mini 0131 input Data Zone Tokens",
            "text embedding 3 large DZ Tokens",
        ],
    )
    def test_data_zone_spellings(self, meter):
        key = parse_meter_name(meter)
        assert key is not None, meter
        assert key.deployment is DeploymentType.DATA_ZONE

    @pytest.mark.parametrize(
        "meter",
        [
            "GPT 5 Mini Inpt Glbl 1M Tokens",
            "GPT 5.1 inp Gl 1M Tokens",
            "5.6 luna ShortCo Inp Std Gl 1M Tokens",
        ],
    )
    def test_global_spellings(self, meter):
        assert parse_meter_name(meter).deployment is DeploymentType.GLOBAL

    def test_meter_without_a_tier_marker_is_regional(self):
        """Azure's older single-tier meters name no deployment; those resources
        are regional, and must not silently be treated as global."""
        assert parse_meter_name("gpt 4.1 Inp Tokens").deployment is DeploymentType.REGIONAL


class TestPriceKind:
    @pytest.mark.parametrize(
        ("meter", "expected"),
        [
            ("GPT 5 Mini Inpt DZone 1M Tokens", PriceKind.INPUT),
            ("GPT 5 Mini outpt DZone 1M Tokens", PriceKind.OUTPUT),
            ("GPT 5 Mini cchd Inpt DZone 1M Tokens", PriceKind.CACHE_READ),
            ("GPT 5.1 cd inp Dz 1M Tokens", PriceKind.CACHE_READ),
            ("5.6 luna ShortCo Cd Inp Std DZ 1M Tokens", PriceKind.CACHE_READ),
            ("5.6 luna ShortCo Cd Wr Std DZ 1M Tokens", PriceKind.CACHE_WRITE),
            ("Rerank v4 Fast DZ Search", PriceKind.SEARCH_UNIT),
        ],
    )
    def test_kinds(self, meter, expected):
        assert parse_meter_name(meter).kind is expected

    def test_cache_write_is_not_read(self):
        """'Cd Wr' and 'Cd Inp' differ by one token and by 12x in price."""
        write = parse_meter_name("5.6 luna ShortCo Cd Wr Std DZ 1M Tokens")
        read = parse_meter_name("5.6 luna ShortCo Cd Inp Std DZ 1M Tokens")
        assert write.kind is PriceKind.CACHE_WRITE
        assert read.kind is PriceKind.CACHE_READ

    def test_embedding_meters_name_no_component(self):
        """Embedding meters carry no input/output token because they only
        consume input. Reading them as 'no kind' drops them from the catalog
        entirely — which is how they were missed the first time."""
        key = parse_meter_name("text embedding 3 small DZ Tokens")
        assert key is not None
        assert key.kind is PriceKind.INPUT
        assert key.model == "text-embedding-3-small"


class TestModeAndContext:
    def test_batch_and_priority(self):
        assert parse_meter_name("5.4 Batch inp Dz 1M Tokens").mode is BillingMode.BATCH
        assert parse_meter_name("5.4 pp inp Dz 1M Tokens").mode is BillingMode.PRIORITY
        assert parse_meter_name("5.4 inp Dz 1M Tokens").mode is BillingMode.STANDARD

    def test_context_tier(self):
        assert parse_meter_name("5.4 longco inp Dz 1M Tokens").context is ContextTier.LONG
        assert parse_meter_name("5.4 inp Dz 1M Tokens").context is ContextTier.SHORT

    def test_camel_cased_context_survives_tokenisation(self):
        """'ShortCo' is camelCase-split before tokenising; without a fixup the
        halves leak into the model name as 'gpt-5.5-short-co'."""
        key = parse_meter_name("5.5 ShortCo inp Dz 1M Tokens")
        assert key.model == "gpt-5.5"
        assert key.context is ContextTier.SHORT
        assert parse_meter_name("5.5 LongCo inp Dz 1M Tokens").context is ContextTier.LONG

    def test_camel_cased_glue_is_split(self):
        key = parse_meter_name("gpt 4o mini0718 BatchOutp DataZone Tokens")
        assert key.kind is PriceKind.OUTPUT
        assert key.mode is BillingMode.BATCH
        assert key.deployment is DeploymentType.DATA_ZONE


class TestModelNames:
    @pytest.mark.parametrize(
        ("meter", "model"),
        [
            ("GPT 5 Mini Inpt DZone 1M Tokens", "gpt-5-mini"),
            ("GPT 5 Nano Inpt DZone 1M Tokens", "gpt-5-nano"),
            ("GPT 5.1 inp Dz 1M Tokens", "gpt-5.1"),
            ("GPT 5.1 chat inp Dz 1M Tokens", "gpt-5.1-chat"),
            ("5.4 inp Dz 1M Tokens", "gpt-5.4"),
            ("5.4 mini Inp Dz 1M Tokens", "gpt-5.4-mini"),
            ("5.6 luna ShortCo Inp Std DZ 1M Tokens", "gpt-5.6-luna"),
            ("5.6 terra LongCo Inp Std DZ 1M Tokens", "gpt-5.6-terra"),
            ("Rerank v4 Fast DZ Search", "cohere-rerank-v4-fast"),
        ],
    )
    def test_canonical_ids(self, meter, model):
        assert parse_meter_name(meter).model == model

    def test_chat_variant_is_a_separate_model(self):
        """'GPT 5.1' and 'GPT 5.1 chat' are different models at different
        prices; collapsing them would mis-price both."""
        assert parse_meter_name("GPT 5.1 inp Dz 1M Tokens").model == "gpt-5.1"
        assert parse_meter_name("GPT 5.1 chat inp Dz 1M Tokens").model == "gpt-5.1-chat"


class TestUnitNormalisation:
    def test_per_million_and_per_thousand_agree(self):
        catalog = [
            _row("A inp Dz 1M Tokens", 2.0, unit="1M"),
            _row("B inp Dz Tokens", 0.002, unit="1K"),
        ]
        table, _ = parse_catalog(catalog)
        a = table[("a", "input", "data_zone", "standard", "short")]
        b = table[("b", "input", "data_zone", "standard", "short")]
        assert a.unit_price == b.unit_price == Decimal("0.000002")


class TestSupersededPrices:
    """Azure's feed keeps old prices alongside current ones. Picking the wrong
    row is silent and produces a plausible-looking number."""

    def test_latest_effective_price_wins(self):
        catalog = [
            _row("5.4 mini Inp Dz 1M Tokens", 0.825, effective="2026-03-01"),
            _row("5.4 mini Inp Dz 1M Tokens", 0.9, effective="2026-06-01"),
        ]
        table, _ = parse_catalog(catalog, as_of="2026-07-31")
        meter = table[("gpt-5.4-mini", "input", "data_zone", "standard", "short")]
        assert meter.unit_price * 1_000_000 == Decimal("0.9")

    def test_row_order_does_not_matter(self):
        rows = [
            _row("5.4 mini Inp Dz 1M Tokens", 0.9, effective="2026-06-01"),
            _row("5.4 mini Inp Dz 1M Tokens", 0.825, effective="2026-03-01"),
        ]
        table, _ = parse_catalog(rows, as_of="2026-07-31")
        key = ("gpt-5.4-mini", "input", "data_zone", "standard", "short")
        assert table[key].unit_price * 1_000_000 == Decimal("0.9")

    def test_future_dated_price_does_not_apply_yet(self):
        """An announced-but-not-yet-effective price must not be billed early."""
        catalog = [
            _row("5.6 luna ShortCo Inp Std DZ 1M Tokens", 1.1, effective="2026-07-01"),
            _row("5.6 luna ShortCo Inp Std DZ 1M Tokens", 0.22, effective="2026-09-01"),
        ]
        table, _ = parse_catalog(catalog, as_of="2026-07-31")
        key = ("gpt-5.6-luna", "input", "data_zone", "standard", "short")
        assert table[key].unit_price * 1_000_000 == Decimal("1.1")

    def test_as_of_can_look_forward(self):
        catalog = [
            _row("5.6 luna ShortCo Inp Std DZ 1M Tokens", 1.1, effective="2026-07-01"),
            _row("5.6 luna ShortCo Inp Std DZ 1M Tokens", 0.22, effective="2026-09-01"),
        ]
        table, _ = parse_catalog(catalog, as_of="2026-09-15")
        key = ("gpt-5.6-luna", "input", "data_zone", "standard", "short")
        assert table[key].unit_price * 1_000_000 == Decimal("0.22")


class TestSkippingAndReporting:
    def test_non_token_meters_are_skipped_not_failed(self):
        catalog = [
            _row("Sora 2 pro glbl Second", 0.5, unit="1", product="Azure OpenAI"),
            _row("Provisioned Managed Data Zone Unit", 1.0, unit="1"),
            _row("GPT 5 Mini Inpt DZone 1M Tokens", 0.275),
        ]
        _table, report = parse_catalog(catalog)
        assert report.parsed == 1
        assert report.skipped == 2
        assert report.failed == []

    def test_unclassifiable_meters_are_reported(self):
        """A meter we cannot read must be loud. Silently dropping it records a
        $0 cost against a real invoice."""
        catalog = [_row("Something Entirely New Dz 1M Tokens", 1.0)]
        _table, report = parse_catalog(catalog)
        assert report.failed == ["Something Entirely New Dz 1M Tokens"]
        assert not report.ok

    def test_zero_priced_meters_carry_no_signal(self):
        catalog = [_row("GPT 5 Mini Inpt DZone 1M Tokens", 0)]
        table, report = parse_catalog(catalog)
        assert table == {}
        assert report.skipped == 1

    def test_products_outside_the_filter_are_ignored(self):
        catalog = [_row("Some Meter inp Dz 1M Tokens", 1.0, product="Azure Kubernetes")]
        table, report = parse_catalog(catalog)
        assert table == {}
        assert report.parsed == 0
