"""Core value types: what a price is, and what you have to tell us to get one."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal


class DeploymentType(enum.StrEnum):
    """Which Azure deployment tier a resource runs on.

    This is the whole reason this library exists. Azure meters the same model
    differently per tier, and the premium is not a fixed percentage — it is
    10% on most meters and 20% on others. Every "OpenAI price" published
    elsewhere is the Global one.
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


@dataclass(frozen=True)
class Meter:
    """A parsed meter and its per-token (or per-unit) price."""

    key: MeterKey
    #: USD per single token / search unit — already divided out of Azure's
    #: "1M Tokens" or "1K" unit of measure, so it composes without surprises.
    unit_price: Decimal
    #: Azure's raw values, kept for provenance and debugging.
    meter_name: str
    product_name: str
    unit_of_measure: str
    retail_price: Decimal
    effective_start_date: str = ""


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
    #: Meters actually used, for auditing a surprising number.
    meters_used: list[str] = field(default_factory=list)

    @property
    def total_cost(self) -> Decimal:
        return self.input_cost + self.output_cost


class AzurePricesError(Exception):
    """Base class for this library's errors."""


class ModelNotFound(AzurePricesError):
    """No meter matched the requested model."""


class PriceNotFound(AzurePricesError):
    """The model exists but not on the requested deployment/mode/context."""
