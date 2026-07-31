"""Core value types: what a price is, and what you have to tell us to get one."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal


class DeploymentType(enum.StrEnum):
    """Which Azure deployment tier a resource runs on.

    This is the whole reason this library exists. Azure meters the same model
    differently per tier, and every "OpenAI price" published elsewhere is the
    Global one. Note that a tier is not a single price either: the EU/US data
    zones sit 10% over Global and APAC sits 20% over, so `DATA_ZONE` needs a
    region whenever the two disagree — see `Meter.price_for`.
    """

    GLOBAL = "global"
    DATA_ZONE = "data_zone"
    REGIONAL = "regional"


class BillingMode(enum.StrEnum):
    """Processing tier. Azure prices each on its own meter."""

    STANDARD = "standard"
    BATCH = "batch"  # ~50% of standard
    PRIORITY = "priority"  # "PP" in Azure meter names; ~2x standard


class ContextTier(enum.StrEnum):
    """Short- vs long-context meters.

    Azure bills a request whose prompt crosses the long-context threshold
    WHOLLY on the long meters — input and output alike. It is not a per-token
    split across the boundary.
    """

    SHORT = "short"
    LONG = "long"


class PriceKind(enum.StrEnum):
    """Which component of a request a meter prices."""

    INPUT = "input"
    CACHE_READ = "cache_read"
    CACHE_WRITE = "cache_write"
    OUTPUT = "output"
    SEARCH_UNIT = "search_unit"  # rerank models bill per 1000 search units


#: Prompt size at which Azure switches a request to its long-context meters.
DEFAULT_LONG_CONTEXT_THRESHOLD = 272_000


class AzurePricesError(Exception):
    """Base class for this library's errors."""


class ModelNotFound(AzurePricesError):
    """No meter matched the requested model."""


class PriceNotFound(AzurePricesError):
    """The model exists but not on the requested deployment/mode/context."""


class AmbiguousRegionPrice(AzurePricesError):
    """The meter is priced differently per region and no region was given.

    Azure runs several data zones at different rates, so "the Data Zone price"
    is not a single number. Guessing one would silently misprice every call
    made from the other zone.
    """


@dataclass(frozen=True)
class MeterKey:
    """Identity of a single Azure meter, parsed out of its display name."""

    model: str
    kind: PriceKind
    deployment: DeploymentType
    mode: BillingMode = BillingMode.STANDARD
    context: ContextTier = ContextTier.SHORT

    def as_tuple(self) -> tuple[str, str, str, str, str]:
        return (
            self.model,
            self.kind.value,
            self.deployment.value,
            self.mode.value,
            self.context.value,
        )


@dataclass
class Meter:
    """A parsed meter and its per-unit price(s).

    A meter does not have *one* price. Azure runs several data zones and
    charges differently between them: ``5.4 mini Inp Dz`` is $0.825/M across
    the US and EU regions and $0.90/M across APAC — same meter, same effective
    date, 9% apart. Prices are therefore held per region and only collapsed
    when every region agrees.
    """

    key: MeterKey
    #: region -> USD per single token / search unit, already divided out of
    #: Azure's "1M Tokens" / "1K" unit so the numbers compose without
    #: surprises. The "" key holds rows Azure reported with no region.
    region_prices: dict[str, Decimal] = field(default_factory=dict)
    #: Azure's raw values, kept for provenance and debugging.
    meter_name: str = ""
    product_name: str = ""
    unit_of_measure: str = ""
    #: region -> the date that region's price took effect.
    region_effective_dates: dict[str, str] = field(default_factory=dict)

    @property
    def regions(self) -> list[str]:
        return sorted(r for r in self.region_prices if r)

    @property
    def is_region_dependent(self) -> bool:
        return len(set(self.region_prices.values())) > 1

    @property
    def unit_price(self) -> Decimal:
        """The single price, when every region agrees.

        Raises when they do not, rather than returning one of them: picking
        silently is how a bill and a cost report drift apart unnoticed.
        """
        prices = set(self.region_prices.values())
        if not prices:
            raise PriceNotFound(f"Meter {self.meter_name!r} carries no price")
        if len(prices) > 1:
            raise AmbiguousRegionPrice(
                f"{self.meter_name!r} is priced differently per region "
                f"({self._price_summary()}). Pass region= to pick one."
            )
        return next(iter(prices))

    def price_for(self, region: str | None) -> Decimal:
        """The price in ``region``, or the unambiguous price if None.

        A region **disambiguates**; it does not restrict. An unrecognised one
        still returns the price when every region agrees, because Azure's
        published region list lags real availability and refusing to price a
        call whose answer is not in doubt would be false precision.
        """
        if region:
            key = region.strip().lower()
            if key in self.region_prices:
                return self.region_prices[key]
            if self.is_region_dependent:
                raise PriceNotFound(
                    f"{self.meter_name!r} is priced per region and {region!r} is not "
                    f"among them. Available: {', '.join(self.regions) or '(none)'}"
                )
        return self.unit_price

    def effective_start_date_for(self, region: str | None = None) -> str:
        if region:
            return self.region_effective_dates.get(region.strip().lower(), "")
        dates = set(self.region_effective_dates.values())
        return next(iter(dates)) if len(dates) == 1 else max(dates, default="")

    def _price_summary(self) -> str:
        by_price: dict[Decimal, list[str]] = {}
        for region, price in sorted(self.region_prices.items()):
            by_price.setdefault(price, []).append(region or "(no region)")
        return "; ".join(
            f"{price} in {', '.join(regions[:3])}"
            + (f" +{len(regions) - 3} more" if len(regions) > 3 else "")
            for price, regions in sorted(by_price.items())
        )


@dataclass
class Usage:
    """Token counts from a provider response.

    ``cache_read_tokens`` are counted as part of ``input_tokens`` (that is how
    both OpenAI and Azure report them), so they are subtracted out rather than
    charged twice.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    search_units: float = 0

    def __post_init__(self) -> None:
        # A negative count is always a bug upstream; passing it through would
        # produce a negative cost that quietly cancels out real spend in an
        # aggregate.
        for field_name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "search_units",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} cannot be negative")

        if self.cache_read_tokens > self.input_tokens:
            # Defensive: a provider that reports cached tokens separately
            # rather than as a subset would otherwise produce negative
            # uncached volume and a negative bill.
            self.cache_read_tokens = self.input_tokens


@dataclass
class Price:
    """The result of a price calculation."""

    input_cost: Decimal = Decimal("0")
    output_cost: Decimal = Decimal("0")
    model: str = ""
    deployment: DeploymentType = DeploymentType.GLOBAL
    mode: BillingMode = BillingMode.STANDARD
    context: ContextTier = ContextTier.SHORT
    #: The region the rates were taken from, when one was specified.
    region: str | None = None
    #: Meters actually used, for auditing a surprising number.
    meters_used: list[str] = field(default_factory=list)

    @property
    def total_cost(self) -> Decimal:
        return self.input_cost + self.output_cost
