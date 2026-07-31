# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Documentation

- Corrected the Data Zone premium description. It is uniform *within* a zone —
  EU/US at +10% over Global, APAC at +20% — not "10% on some meters and 20% on
  others". The earlier wording came from comparing Global against APAC rows.

## [0.1.0] - 2026-07-31

Initial release.

### Added

- `calc_price(usage, model=..., deployment=..., mode=...)` returning a `Price` with
  `input_cost`, `output_cost`, `total_cost` and `meters_used`.
- `Usage` input type covering input, output and cache-read tokens.
- `DeploymentType` tiers (`GLOBAL`, `DATA_ZONE`, `REGIONAL`) so Data Zone deployments
  bill against their own Azure meters rather than OpenAI Global list prices — a
  10–20% difference that varies per meter.
- `BillingMode` support for `STANDARD`, `BATCH` and `PRIORITY` meters.
- Long-context billing via `ContextTier`: requests past the long-context threshold
  bill wholly on Azure's LongCo meters for both input and output.
- Price sourcing from the public Azure retail price list API
  (<https://prices.azure.com/api/retail/prices>), with no authentication required.
- Optional Redis-backed sync — `fetch_and_store_prices()`, `load_prices_from_redis()`
  and `refresh_from_azure()` — using the keys `azure_genai_prices:data` and
  `azure_genai_prices:updated_at`, available via the `redis` extra.
- Bundled price snapshot so the package works fully offline out of the box.
- Region-aware pricing. Azure's data zones are **not priced alike**:
  `5.4 mini Inp Dz` is $0.825/M across the EU and US and $0.90/M across APAC on
  the same effective date, and 187 meters vary this way. `calc_price(...,
  region=...)` selects one; without a region, a meter whose price varies raises
  `AmbiguousRegionPrice` rather than guessing. A region disambiguates rather
  than restricts — an unrecognised one still prices where every region agrees.
  `list_regions(model)` and `Meter.is_region_dependent` expose the dimension.
- Superseded prices are resolved per region. Azure keeps old rows in the feed
  alongside current ones, so an APAC repricing must not overwrite the EU price,
  and row order must not decide the answer. `parse_catalog(as_of=...)` prices
  the catalog as of any date, which makes an upcoming repricing testable.
- Introspection helpers `list_models()` and `get_meters(model)`, plus the
  `ModelNotFound`, `PriceNotFound` and `AmbiguousRegionPrice` exceptions.
- CLI with `refresh`, `price`, `models` and `coverage` subcommands.

### Notes

- Every path that cannot find a rate raises rather than returning zero. A $0
  cost recorded against a real invoice is the failure this package exists to
  prevent, so cached-only and cache-write-only usage on a deployment with no
  meters is an error, not free. `Usage` likewise rejects negative token counts
  instead of producing a negative cost that cancels real spend out of an
  aggregate.
- Meters that cannot be classified are reported, never dropped. The bundled
  snapshot parses 1024 meters with zero failures, asserted by the test suite so
  that a new Azure naming style fails the build instead of going quiet.

[Unreleased]: https://github.com/nikklavzar/azure-genai-prices/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/nikklavzar/azure-genai-prices/releases/tag/v0.1.0
