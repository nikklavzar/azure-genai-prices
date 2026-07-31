# azure-genai-prices

Accurate cost calculation for **Azure AI Foundry / Azure OpenAI** deployments.

Every general-purpose price library ships OpenAI **Global** list prices. Azure meters
each model separately per deployment tier, and a **Data Zone** deployment costs
10–20% more than Global. If you cost an Azure Data Zone deployment against OpenAI
list prices, you under-report your spend — and the error is not a flat percentage
you can multiply out afterwards.

This package reads Azure's own public retail price list and bills against the meter
your deployment actually uses.

## Install

```bash
pip install azure-genai-prices
```

Optional Redis-backed price sync:

```bash
pip install azure-genai-prices[redis]
```

The package ships a bundled price snapshot, so it works offline with no
configuration.

## Quick start

```python
from azure_genai_prices import Usage, calc_price, DeploymentType, BillingMode

usage = Usage(input_tokens=100_000, output_tokens=2_000, cache_read_tokens=80_000)

price = calc_price(
    usage,
    model="gpt-5.6-luna",
    deployment=DeploymentType.DATA_ZONE,  # or .GLOBAL / .REGIONAL; default GLOBAL
)

price.input_cost  # Decimal
price.output_cost  # Decimal
price.total_cost  # Decimal
price.meters_used  # list[str] — the Azure meter names actually applied
```

Batch and Priority Processing deployments use different meters:

```python
price = calc_price(usage, model="gpt-5.4", mode=BillingMode.BATCH)
```

`BillingMode.STANDARD` is the default; `BATCH` bills at roughly 50% and `PRIORITY`
at roughly 2x, per Azure's published meters — the library uses the real published
rate, not a multiplier.

Not every model is offered on every mode: `gpt-5.6-luna` has Standard and
Priority meters but no Batch one. Asking for a mode Azure does not publish
raises `PriceNotFound` rather than quietly falling back to a rate you are not
billed at.

Other public names: `ContextTier`, `Price`, `Meter`, `ModelNotFound`,
`PriceNotFound`, `list_models()`, `get_meters(model)`.

## Why not genai-prices?

`genai-prices` and similar libraries are excellent for OpenAI-direct, Anthropic and
the rest. They publish OpenAI's Global list price for a model. Azure prices the same
model differently depending on where it is deployed, and the Data Zone premium
varies per meter:

| Model | Meter | Global | Data Zone (EU/US) | Data Zone (APAC) |
|---|---|---|---|---|
| gpt-5.6-luna | input | $1.00 / M | $1.10 / M | not offered |
| gpt-5.4-mini | input | $0.75 / M | $0.825 / M | $0.90 / M |
| gpt-5.1 | input | $1.25 / M | $1.375 / M | $1.50 / M |

The point that table should make: **"the Data Zone price" is not one
number.** Azure runs several data zones and does not price them alike. Within a
zone the premium over Global is uniform — the EU and US zones sit at exactly
+10% on every meter, APAC at +20% — but the zones differ from each other by 9%
on the same meter on the same date, and the retail feed distinguishes them only
by region. Read a rate without pinning the region and you get whichever row the
API returned first.

## Regions

Where a meter is priced differently between data zones, `calc_price` refuses to
guess:

```python
from azure_genai_prices import AmbiguousRegionPrice

calc_price(usage, model="gpt-5.4-mini", deployment=DeploymentType.DATA_ZONE)
# AmbiguousRegionPrice: '5.4 mini Inp Dz 1M Tokens' is priced differently per
# region (8.25E-7 in centralus, eastus, eastus2 +11 more; 9E-7 in australiaeast,
# …). Pass region= to pick one.

calc_price(
    usage, model="gpt-5.4-mini", deployment=DeploymentType.DATA_ZONE, region="northeurope"
)  # $0.825 / M
```

Pass `region` as the ARM region id of the deployment (`northeurope`,
`swedencentral`, `japaneast`, …). It **disambiguates rather than restricts**: for
the ~85% of meters priced identically everywhere, an unrecognised region still
returns the price, because Azure's published region list lags real availability.
`list_regions(model)` shows what a model is sold in, and `Meter.is_region_dependent`
flags the ones where it matters.

## Deployment tiers

`DeploymentType` maps to how the deployment was created in Azure AI Foundry:

| Value | Azure deployment |
|---|---|
| `GLOBAL` | Global Standard / Global Provisioned — cheapest, no residency guarantee |
| `DATA_ZONE` | Data Zone Standard — EU/US zones at +10% over Global, APAC at +20%, and **not priced alike between zones** (see [Regions](#regions)) |
| `REGIONAL` | Regional (single-region) Standard |

Pick the one that matches the deployment your requests actually go to. Using the
wrong tier is the single most common source of cost drift.

## Long-context billing

Azure bills long-context requests on separate "LongCo" meters. A request whose
prompt exceeds roughly 272k tokens bills **wholly** on the higher long-context
meters — both input and output — rather than splitting per token at the boundary.
`calc_price` detects the tier from the usage you pass and selects the correct
meters; `price.meters_used` shows which ones were applied, and `ContextTier` is
exposed if you need to reason about the boundary yourself.

## Redis sync

The intended production shape is one scheduled job that fetches prices and stores
them, with every worker process loading the shared snapshot at startup instead of
calling the Azure API itself.

```python
from azure_genai_prices import (
    fetch_and_store_prices,
    load_prices_from_redis,
    refresh_from_azure,
)

fetch_and_store_prices(redis_url="redis://localhost:6379/2")  # scheduled job
load_prices_from_redis(redis_url="redis://localhost:6379/2")  # process startup
refresh_from_azure()  # no Redis: straight into memory
```

Keys written: `azure_genai_prices:data` and `azure_genai_prices:updated_at`.

Redis is an optional extra. If you call none of these, the bundled snapshot is used.

## CLI

```bash
azure-genai-prices refresh [--output PATH]
azure-genai-prices price gpt-5.6-luna --input-tokens 100000 --output-tokens 2000 --deployment data-zone
azure-genai-prices models [--filter TEXT] [-v]
azure-genai-prices price gpt-5.4-mini --input-tokens 1000000 --deployment data-zone --region northeurope
azure-genai-prices coverage
```

`coverage` reports how many Azure meters parsed cleanly, how many were skipped as
out of scope, and how many failed to parse — useful when Azure introduces a new
meter naming pattern.

## How prices are sourced

Prices come from Azure's public retail price list API,
<https://prices.azure.com/api/retail/prices> — no authentication, no API key, no
subscription required.

Azure only ever changes these prices on the **first of a month**: across 20,390
meter rows, zero carry a non-month-start effective date. A daily (or even
six-hourly) refresh is therefore more than sufficient; there is no benefit to
polling more aggressively.

## Limitations

- **Prices are retail.** Enterprise agreements, committed-use discounts, reservations
  and private pricing are not visible in the public API and will differ from what
  this library reports.
- **Azure sets its own rates and lags OpenAI.** When OpenAI changes a list price,
  Azure may follow later, or not at all, or at a different number. Do not treat this
  library's output as a proxy for OpenAI-direct pricing.
- **Meter coverage is limited to generative AI meters** that the parser recognises.
  Run `azure-genai-prices coverage` to see what was skipped. Fine-tuning, Sora
  video, realtime audio and the retired flat-rate completion models are out of
  scope by design — none of them is a per-token inference rate.
- Currency is USD as published by the retail API.

## Releasing

Releases publish to PyPI through GitHub Actions using Trusted Publishing, so no
API token is stored in the repo or in CI. `.github/workflows/release.yml` runs
the test suite, builds, and uploads — triggered by publishing a GitHub Release
(or manually via `workflow_dispatch`). Bump `version` in `pyproject.toml` and
add a `CHANGELOG.md` entry first.

## Contributing

Issues and pull requests are welcome. To work on the package:

```bash
uv sync --all-extras
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

CI runs the same commands on Python 3.11, 3.12 and 3.13. New pricing behaviour
should come with a test, and meter-parsing changes should keep
`azure-genai-prices coverage` at or above its current numbers.

## License

MIT — see [LICENSE](LICENSE).
