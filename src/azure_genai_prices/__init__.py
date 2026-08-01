"""Azure AI Foundry / Azure OpenAI pricing, including Data Zone deployments.

General-purpose price libraries publish OpenAI *Global* list prices. Azure
meters each model per deployment tier, and Data Zone costs 10-20% more — how
much depends on which data zone, so it cannot be derived by scaling. This
library reads the real meters from Azure's public retail price list.

    >>> from azure_genai_prices import Usage, calc_price, DeploymentType
    >>> price = calc_price(
    ...     Usage(input_tokens=1_000_000),
    ...     model="gpt-5.6-luna",
    ...     deployment=DeploymentType.DATA_ZONE,
    ... )
    >>> price.total_cost
    Decimal('1.10')
"""

from __future__ import annotations

from .calc import calc_price
from .calc import get_meters
from .calc import has_long_context_pricing
from .calc import list_models
from .calc import list_regions
from .meters import ParseReport
from .meters import parse_catalog
from .meters import parse_meter_name
from .store import fetch_and_store_prices
from .store import get_report
from .store import get_source
from .store import is_loaded
from .store import load_bundled_snapshot
from .store import load_items
from .store import load_prices_from_redis
from .store import refresh_from_azure
from .store import reset
from .store import snapshot_updated_at
from .types import DEFAULT_LONG_CONTEXT_THRESHOLD
from .types import AmbiguousRegionPrice
from .types import AzurePricesError
from .types import BillingMode
from .types import ContextTier
from .types import DeploymentType
from .types import Meter
from .types import MeterKey
from .types import ModelNotFound
from .types import Price
from .types import PriceKind
from .types import PriceNotFound
from .types import Usage

__version__ = "0.1.1"

__all__ = [
    "DEFAULT_LONG_CONTEXT_THRESHOLD",
    "AmbiguousRegionPrice",
    "AzurePricesError",
    "BillingMode",
    "ContextTier",
    "DeploymentType",
    "Meter",
    "MeterKey",
    "ModelNotFound",
    "ParseReport",
    "Price",
    "PriceKind",
    "PriceNotFound",
    "Usage",
    "__version__",
    "calc_price",
    "fetch_and_store_prices",
    "get_meters",
    "get_report",
    "get_source",
    "has_long_context_pricing",
    "is_loaded",
    "list_models",
    "list_regions",
    "load_bundled_snapshot",
    "load_items",
    "load_prices_from_redis",
    "parse_catalog",
    "parse_meter_name",
    "refresh_from_azure",
    "reset",
    "snapshot_updated_at",
]
