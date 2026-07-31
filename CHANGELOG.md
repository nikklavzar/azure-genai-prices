# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
- Introspection helpers `list_models()` and `get_meters(model)`, plus the
  `ModelNotFound` and `PriceNotFound` exceptions.
- CLI with `refresh`, `price`, `models` and `coverage` subcommands.

[Unreleased]: https://github.com/nikklavzar/azure-genai-prices/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/nikklavzar/azure-genai-prices/releases/tag/v0.1.0
