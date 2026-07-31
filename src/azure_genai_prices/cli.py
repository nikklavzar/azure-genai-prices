"""Command line interface: ``azure-genai-prices <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from . import __version__
from .calc import calc_price
from .calc import get_meters
from .calc import list_models
from .fetch import fetch_price_items
from .store import get_report
from .store import get_source
from .store import load_items
from .types import BillingMode
from .types import DeploymentType
from .types import Usage


def _cmd_refresh(args: argparse.Namespace) -> int:
    items = fetch_price_items()
    report = load_items(items, "azure retail api")
    payload = {"fetched_at": datetime.now().isoformat(), "items": items}

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
        print(f"Wrote {len(items)} meter rows to {args.output}")
    else:
        print(f"Fetched {len(items)} meter rows (not written; pass --output)")

    print(f"Parsed {report.parsed} meters, skipped {report.skipped}")
    if report.failed:
        print(f"Unclassified: {len(report.failed)}", file=sys.stderr)
        for name in sorted(report.failed):
            print(f"  {name}", file=sys.stderr)
    return 0


def _cmd_price(args: argparse.Namespace) -> int:
    price = calc_price(
        Usage(
            input_tokens=args.input_tokens,
            output_tokens=args.output_tokens,
            cache_read_tokens=args.cached_tokens,
        ),
        model=args.model,
        deployment=DeploymentType(args.deployment.replace("-", "_")),
        mode=BillingMode(args.mode),
    )
    print(f"model      {price.model}")
    tiers = f"{price.deployment.value}  mode {price.mode.value}"
    print(f"deployment {tiers}  context {price.context.value}")
    print(f"input      ${price.input_cost:.6f}")
    print(f"output     ${price.output_cost:.6f}")
    print(f"total      ${price.total_cost:.6f}")
    print("meters:")
    for meter in price.meters_used:
        print(f"  {meter}")
    return 0


def _cmd_models(args: argparse.Namespace) -> int:
    for model in list_models():
        if args.filter and args.filter.lower() not in model:
            continue
        if args.verbose:
            meters = get_meters(model)
            tiers = sorted({m.key.deployment.value for m in meters})
            print(f"{model:32} {len(meters):3} meters  tiers: {', '.join(tiers)}")
        else:
            print(model)
    return 0


def _cmd_coverage(_args: argparse.Namespace) -> int:
    list_models()  # force a load
    report = get_report()
    print(f"source:  {get_source()}")
    if report is None:
        print("no snapshot loaded")
        return 1
    print(f"parsed:  {report.parsed}")
    print(f"skipped: {report.skipped}")
    print(f"failed:  {len(report.failed)}")
    for name in sorted(report.failed):
        print(f"  {name}")
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="azure-genai-prices",
        description="Azure AI Foundry / Azure OpenAI pricing, Data Zone included.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_refresh = sub.add_parser("refresh", help="fetch the live Azure price list")
    p_refresh.add_argument("--output", help="write the snapshot JSON here")
    p_refresh.set_defaults(func=_cmd_refresh)

    p_price = sub.add_parser("price", help="price a request")
    p_price.add_argument("model")
    p_price.add_argument("--input-tokens", type=int, default=0)
    p_price.add_argument("--output-tokens", type=int, default=0)
    p_price.add_argument("--cached-tokens", type=int, default=0)
    p_price.add_argument(
        "--deployment",
        default="global",
        choices=["global", "data-zone", "data_zone", "regional"],
    )
    p_price.add_argument("--mode", default="standard", choices=[m.value for m in BillingMode])
    p_price.set_defaults(func=_cmd_price)

    p_models = sub.add_parser("models", help="list priceable models")
    p_models.add_argument("--filter", help="substring match")
    p_models.add_argument("-v", "--verbose", action="store_true")
    p_models.set_defaults(func=_cmd_models)

    p_cov = sub.add_parser("coverage", help="show meter parse coverage")
    p_cov.set_defaults(func=_cmd_coverage)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
