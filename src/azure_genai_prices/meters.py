"""Parsing Azure meter names into structured prices.

Azure's meter names are terse, abbreviated, and inconsistent between model
generations — the same concept appears half a dozen ways::

    GPT 5 Mini cchd Inpt DZone 1M Tokens     -> gpt-5-mini,     cache read, data zone
    GPT 5.1 cd inp Dz 1M Tokens              -> gpt-5.1,        cache read, data zone
    5.4 mini Inp Dz 1M Tokens                -> gpt-5.4-mini,   input,      data zone
    5.6 luna ShortCo Cd Wr Std DZ 1M Tokens  -> gpt-5.6-luna,   cache write, data zone
    text embedding 3 large DZ Tokens         -> text-embedding-3-large,    data zone
    Rerank v4 Fast DZ Search                 -> cohere-rerank-v4-fast, search unit

Rather than a regex per generation — which rots the moment Azure ships a new
naming style — the parser tokenises the name and classifies each token against
a vocabulary. Whatever tokens are left over are the model name.

Meters we cannot classify are **reported, never silently dropped**: a missing
meter means a $0 cost recorded against a real invoice, which is the failure
mode this whole library exists to prevent. See `parse_catalog`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from decimal import Decimal

from .types import BillingMode
from .types import ContextTier
from .types import DeploymentType
from .types import Meter
from .types import MeterKey
from .types import PriceKind

# --- token vocabularies ----------------------------------------------------
# Order matters only within a category; the parser checks longest-first.

_DEPLOYMENT_TOKENS: dict[str, DeploymentType] = {
    "dzone": DeploymentType.DATA_ZONE,
    "datazone": DeploymentType.DATA_ZONE,
    "dz": DeploymentType.DATA_ZONE,
    "glbl": DeploymentType.GLOBAL,
    "global": DeploymentType.GLOBAL,
    "gl": DeploymentType.GLOBAL,
    "regnl": DeploymentType.REGIONAL,
    "rgnl": DeploymentType.REGIONAL,
    "regional": DeploymentType.REGIONAL,
}

_MODE_TOKENS: dict[str, BillingMode] = {
    "batch": BillingMode.BATCH,
    "pp": BillingMode.PRIORITY,
    "std": BillingMode.STANDARD,
}

_CONTEXT_TOKENS: dict[str, ContextTier] = {
    "longco": ContextTier.LONG,
    "shortco": ContextTier.SHORT,
}

_INPUT_TOKENS = {"inp", "inpt", "input", "in"}
_OUTPUT_TOKENS = {"opt", "outp", "outpt", "out", "output"}
_CACHED_TOKENS = {"cchd", "cd", "cached", "cach"}
_WRITE_TOKENS = {"wr", "write"}
_SEARCH_TOKENS = {"search"}

# Noise that carries no pricing meaning.
_DROP_TOKENS = {"tokens", "token", "1m", "1k", "unit", "units", "gpt"}

#: Leftover-token fixups, applied after the model name is assembled. Keeps the
#: general parser honest instead of teaching it about individual products.
_MODEL_ALIASES: dict[str, str] = {
    "embedding-ada": "text-embedding-ada-002",
    "ada": "text-embedding-ada-002",
    "text-embedding-3-large-grader": "text-embedding-3-large",
    "rerank-v4-fast": "cohere-rerank-v4-fast",
    "rerank-v4-pro": "cohere-rerank-v4-pro",
    "embed-v4-txt": "cohere-embed-v4",
    "command-a": "cohere-command-a",
    "command-a-plus": "cohere-command-a-plus",
    "embed-v4-img": "cohere-embed-v4-image",
    # Azure spells the same GPT-4o snapshot both glued and hyphenated.
    "gpt-4o-mini0718": "gpt-4o-mini-0718",
    "gpt-4o-0806": "gpt-4o",
}

#: Products whose meters this library understands. Azure files GenAI models
#: under several product names; anything else (Speech, Dall-E, Assistants,
#: provisioned-throughput units) is out of scope and skipped quietly.
DEFAULT_PRODUCTS = (
    "Azure OpenAI",
    "Azure OpenAI GPT5",
    "Azure OpenAI Embedding",
    "Cohere Models",
)

#: Meters that price something other than per-token inference. Skipped without
#: being counted as parse failures.
_NON_TOKEN_MARKERS = (
    "provisioned",
    "ptu",
    "dall-e",
    "image",
    "speech",
    "whisper",
    "assistants",
    "code-interpreter",
    "session",
    "characters",
    "file-search",
    "file search",
    "hosting",
    "hstng",
    "grader",
    # A model-less catch-all meter that prices no inference.
    "standard unit",
    # Legacy base-completion models bill one flat rate with no input/output
    # split; there is no honest way to map them onto this model.
    "babbage-002",
    "davinci-002",
    "curie-002",
    # Realtime preview prices audio and text as separate components, a
    # dimension this library does not model yet.
    "realtimeprvw",
    "realtime",
    # Video generation is priced per second / per clip, not per token.
    "sora",
    "video",
    # Fine-tuning: training is billed per training token and hosting per hour,
    # neither of which is an inference rate.
    "ft-trng",
    "fine-tune",
    "ft training",
    " ft ",
    # Retired single-rate completion meters (Az-Davinci-002, Az-GPT-3.5-turbo,
    # Az-Embeddings-Ada). They bill one flat rate with no input/output split,
    # so there is no honest way to map them onto this model.
    "az-babbage",
    "az-davinci",
    "az-curie",
    "az-ada",
    "az-gpt",
    "az-embeddings",
)

#: Models whose meters carry no input/output token because they only ever
#: consume input. Azure names them e.g. "text embedding 3 large DZ Tokens".
_INPUT_ONLY_MARKERS = ("embed",)

_SPLIT_RE = re.compile(r"[\s\-_/]+")

#: Azure glues words together in camelCase on some meters ("BatchOutp",
#: "DataZone"), which would otherwise hide a real price behind an
#: unrecognised token.
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")

#: Two-word spellings of single-token concepts, normalised AFTER camelCase
#: expansion — which is itself what splits "ShortCo" into "short co" — and
#: before tokenising.
_PHRASE_FIXUPS = (
    (" data zone", " datazone"),
    ("short co", "shortco"),
    ("long co", "longco"),
)


@dataclass
class ParseReport:
    """What `parse_catalog` made of a raw Azure price list."""

    parsed: int = 0
    skipped: int = 0
    failed: list[str] | None = None

    def __post_init__(self) -> None:
        if self.failed is None:
            self.failed = []

    @property
    def ok(self) -> bool:
        return not self.failed


def _tokenise(meter_name: str) -> list[str]:
    expanded = _CAMEL_RE.sub(" ", meter_name.strip()).lower()
    for phrase, replacement in _PHRASE_FIXUPS:
        expanded = expanded.replace(phrase, replacement)
    return [t for t in _SPLIT_RE.split(expanded) if t]


def _is_skippable(meter_name: str, product_name: str) -> bool:
    blob = f"{product_name} {meter_name}".lower()
    return any(marker in blob for marker in _NON_TOKEN_MARKERS)


def _normalise_model(parts: list[str]) -> str:
    """Turn leftover tokens into a canonical model id.

    Azure writes the same model as "GPT 5 Mini", "gpt-5-mini" and (for newer
    generations) bare "5.4 mini". Everything collapses to the OpenAI-style id.
    """
    name = "-".join(parts).strip("-")
    name = re.sub(r"-+", "-", name)
    if not name:
        return ""
    # Bare version numbers ("5.4-mini", "5-6-luna") are GPT models.
    if re.match(r"^\d", name):
        name = f"gpt-{name}"
    # "gpt-5-4-mini" and "gpt-5-6-luna" are Azure spellings of "gpt-5.4-mini".
    name = re.sub(r"^gpt-(\d)-(\d)(?=-|$)", r"gpt-\1.\2", name)
    return _MODEL_ALIASES.get(name, name)


def parse_meter_name(meter_name: str) -> MeterKey | None:
    """Classify one Azure meter name. Returns None if it prices no known kind."""
    tokens = _tokenise(meter_name)

    deployment: DeploymentType | None = None
    mode = BillingMode.STANDARD
    context = ContextTier.SHORT
    saw_input = saw_output = saw_cached = saw_write = saw_search = False
    leftover: list[str] = []

    for token in tokens:
        if token in _DEPLOYMENT_TOKENS:
            deployment = _DEPLOYMENT_TOKENS[token]
        elif token in _MODE_TOKENS:
            mode = _MODE_TOKENS[token]
        elif token in _CONTEXT_TOKENS:
            context = _CONTEXT_TOKENS[token]
        elif token in _CACHED_TOKENS:
            saw_cached = True
        elif token in _WRITE_TOKENS:
            saw_write = True
        elif token in _INPUT_TOKENS:
            saw_input = True
        elif token in _OUTPUT_TOKENS:
            saw_output = True
        elif token in _SEARCH_TOKENS:
            saw_search = True
        elif token in _DROP_TOKENS:
            continue
        else:
            leftover.append(token)

    # A meter with no deployment marker is Azure's older single-tier naming;
    # those are regional-priced resources.
    if deployment is None:
        deployment = DeploymentType.REGIONAL

    model = _normalise_model(leftover)
    if not model:
        return None

    if saw_search:
        kind = PriceKind.SEARCH_UNIT
    elif saw_cached and saw_write:
        kind = PriceKind.CACHE_WRITE
    elif saw_cached:
        kind = PriceKind.CACHE_READ
    elif saw_output:
        kind = PriceKind.OUTPUT
    elif saw_input:
        kind = PriceKind.INPUT
    elif any(marker in model for marker in _INPUT_ONLY_MARKERS):
        # Embedding meters name no component because there is only one.
        kind = PriceKind.INPUT
    else:
        return None

    return MeterKey(model=model, kind=kind, deployment=deployment, mode=mode, context=context)


def _unit_divisor(unit_of_measure: str) -> Decimal:
    """Azure quotes per "1M"/"1K"/"1" units; normalise to a single unit."""
    unit = (unit_of_measure or "").strip().upper()
    head = unit.split()[0] if unit else "1"
    if head.endswith("M"):
        head = head[:-1] or "1"
        return Decimal(head) * Decimal(1_000_000)
    if head.endswith("K"):
        head = head[:-1] or "1"
        return Decimal(head) * Decimal(1000)
    try:
        return Decimal(head)
    except Exception:
        return Decimal(1)


def _today() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%d")


def _supersedes(candidate: Meter, incumbent: Meter, as_of: str) -> bool:
    """Whether ``candidate`` is the more current price of the two.

    Azure's retail feed returns **superseded rows alongside current ones** —
    ``5.4 mini Inp Dz`` carries both $0.825 (effective 2026-03-01) and $0.90
    (effective 2026-06-01). Taking whichever arrived first yields an arbitrary,
    silently-wrong price, so the row with the latest effective date that has
    actually taken effect wins. Rows dated in the future are announced but not
    yet billable and never win.
    """
    cand, inc = candidate.effective_start_date, incumbent.effective_start_date
    cand_live, inc_live = cand <= as_of, inc <= as_of
    if cand_live != inc_live:
        return cand_live
    if not cand_live:
        # Both are future-dated: keep the one that starts sooner.
        return cand < inc
    return cand > inc


def parse_catalog(
    items: list[dict],
    products: tuple[str, ...] = DEFAULT_PRODUCTS,
    as_of: str | None = None,
) -> tuple[dict[tuple, Meter], ParseReport]:
    """Turn raw Azure retail-price items into a meter table.

    Azure returns one row per meter per region, and — critically — keeps
    superseded prices in the feed. Rows collapse onto the meter key with the
    currently-effective price winning; see `_supersedes`.

    ``as_of`` (``YYYY-MM-DD``) prices the catalog as of a past or future date
    instead of today, which is what makes an upcoming repricing testable.
    """
    as_of = as_of or _today()
    table: dict[tuple, Meter] = {}
    report = ParseReport()

    for item in items:
        product = item.get("productName", "")
        meter_name = item.get("meterName", "")
        if products and not any(product.startswith(p) for p in products):
            continue
        if not meter_name:
            continue
        if _is_skippable(meter_name, product):
            report.skipped += 1
            continue

        key = parse_meter_name(meter_name)
        if key is None:
            report.failed.append(meter_name)
            continue

        retail = Decimal(str(item.get("retailPrice", 0)))
        if retail <= 0:
            # Free/zeroed meters carry no pricing signal.
            report.skipped += 1
            continue

        table_key = key.as_tuple()
        candidate = Meter(
            key=key,
            unit_price=retail / _unit_divisor(item.get("unitOfMeasure", "1")),
            meter_name=meter_name,
            product_name=product,
            unit_of_measure=item.get("unitOfMeasure", ""),
            retail_price=retail,
            effective_start_date=(item.get("effectiveStartDate") or "")[:10],
        )

        incumbent = table.get(table_key)
        if incumbent is None:
            table[table_key] = candidate
            report.parsed += 1
        elif _supersedes(candidate, incumbent, as_of):
            table[table_key] = candidate
        else:
            report.skipped += 1

    return table, report
