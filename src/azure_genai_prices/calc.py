"""Turning usage into a bill."""

from __future__ import annotations

import logging
from decimal import Decimal

from .store import get_table
from .types import DEFAULT_LONG_CONTEXT_THRESHOLD
from .types import BillingMode
from .types import ContextTier
from .types import DeploymentType
from .types import Meter
from .types import ModelNotFound
from .types import Price
from .types import PriceKind
from .types import PriceNotFound
from .types import Usage

logger = logging.getLogger(__name__)


def _canonical(model: str) -> str:
    return model.strip().lower()


def list_models() -> list[str]:
    """Every model id the loaded snapshot can price."""
    return sorted({key[0] for key in get_table()})


def get_meters(model: str) -> list[Meter]:
    """Every meter known for a model — useful for auditing a surprise."""
    wanted = _canonical(model)
    return [meter for key, meter in get_table().items() if key[0] == wanted]


def _lookup(
    model: str,
    kind: PriceKind,
    deployment: DeploymentType,
    mode: BillingMode,
    context: ContextTier,
) -> Meter | None:
    table = get_table()
    meter = table.get((model, kind.value, deployment.value, mode.value, context.value))
    if meter is not None:
        return meter

    # Azure does not publish a long-context meter for every model (the mini
    # tiers, for instance). Those bill at their single rate regardless of
    # prompt size, so fall back rather than refusing to price the call.
    if context is ContextTier.LONG:
        return table.get((model, kind.value, deployment.value, mode.value, ContextTier.SHORT.value))
    return None


def has_long_context_pricing(
    model: str,
    deployment: DeploymentType = DeploymentType.GLOBAL,
    mode: BillingMode = BillingMode.STANDARD,
) -> bool:
    """Whether Azure meters this model's long-context traffic separately."""
    return (
        _canonical(model),
        PriceKind.INPUT.value,
        deployment.value,
        mode.value,
        ContextTier.LONG.value,
    ) in get_table()


def list_regions(model: str) -> list[str]:
    """Azure regions the loaded snapshot prices ``model`` in."""
    regions: set[str] = set()
    for meter in get_meters(model):
        regions.update(meter.regions)
    return sorted(regions)


def calc_price(
    usage: Usage,
    model: str,
    deployment: DeploymentType | str = DeploymentType.GLOBAL,
    mode: BillingMode | str = BillingMode.STANDARD,
    long_context_threshold: int = DEFAULT_LONG_CONTEXT_THRESHOLD,
    context: ContextTier | str | None = None,
    region: str | None = None,
) -> Price:
    """Price one request.

    The context tier is decided from ``usage.input_tokens`` unless ``context``
    forces it. Azure bills a request that crosses the threshold *wholly* on
    the long-context meters — output included — so the tier is chosen once and
    applied to every component, never mixed.

    ``region`` (an ARM region id such as ``"northeurope"``) is required
    whenever Azure prices the model differently between its data zones — the
    EU/US zones and APAC are up to 9% apart on the same meter. Omitting it
    when the price is unambiguous is fine; omitting it when it is not raises
    `AmbiguousRegionPrice` rather than guessing.
    """
    model_id = _canonical(model)
    deployment = DeploymentType(deployment)
    mode = BillingMode(mode)

    if context is not None:
        tier = ContextTier(context)
    else:
        tier = (
            ContextTier.LONG if usage.input_tokens > long_context_threshold else ContextTier.SHORT
        )

    known = {key[0] for key in get_table()}
    if model_id not in known:
        raise ModelNotFound(
            f"No Azure meters for model {model!r}. Try one of: {', '.join(sorted(known)[:5])}…"
        )

    price = Price(model=model_id, deployment=deployment, mode=mode, context=tier, region=region)

    cached = usage.cache_read_tokens
    uncached = usage.input_tokens - cached

    def rate(kind: PriceKind) -> Meter | None:
        return _lookup(model_id, kind, deployment, mode, tier)

    def charge(meter: Meter, units: int | float) -> Decimal:
        cost = Decimal(str(units)) * meter.price_for(region)
        price.meters_used.append(meter.meter_name)
        return cost

    def require(kind: PriceKind, what: str) -> Meter:
        meter = rate(kind)
        if meter is None:
            raise PriceNotFound(
                f"No {deployment.value} / {mode.value} {what} meter for {model!r}"
                + (f" in region {region!r}" if region else "")
                + f". Known meters: {[m.meter_name for m in get_meters(model_id)][:5]}"
            )
        return meter

    if uncached:
        price.input_cost += charge(require(PriceKind.INPUT, "input"), uncached)

    if cached:
        # Falling back to the input rate is correct for a model with no
        # separate cached meter, but a model with NO input meter either means
        # the deployment/mode is wrong — charging nothing would hide that.
        cache_meter = rate(PriceKind.CACHE_READ) or require(PriceKind.INPUT, "cached-input")
        price.input_cost += charge(cache_meter, cached)

    if usage.cache_write_tokens:
        # Only models that meter writes separately (gpt-5.6 and later) charge
        # for them; on everything else a write is included in the input rate
        # and must not be double-billed. Guard against the deployment being
        # wrong entirely, which would otherwise look identical to "free".
        write_meter = rate(PriceKind.CACHE_WRITE)
        if write_meter is not None:
            price.input_cost += charge(write_meter, usage.cache_write_tokens)
        elif rate(PriceKind.INPUT) is None:
            raise PriceNotFound(
                f"No {deployment.value} / {mode.value} meters at all for {model!r}"
                + (f" in region {region!r}" if region else "")
            )

    if usage.output_tokens:
        price.output_cost += charge(require(PriceKind.OUTPUT, "output"), usage.output_tokens)

    if usage.search_units:
        price.input_cost += charge(
            require(PriceKind.SEARCH_UNIT, "search-unit"), usage.search_units
        )

    return price
