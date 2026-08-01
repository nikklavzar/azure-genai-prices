"""The provider adapters shown in the README.

Each provider names its token counts differently, and the README tells people
how to map them. These tests pin those mappings against stubs shaped like the
documented responses, so an example cannot rot into something that silently
prices at zero.

Shapes are taken from the current provider docs:
  Responses API      usage.input_tokens / .output_tokens / .input_tokens_details.cached_tokens
  Chat Completions   usage.prompt_tokens / .completion_tokens / .prompt_tokens_details.*
  LangChain          usage_metadata dict, input_token_details.cache_read
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from azure_genai_prices import DeploymentType
from azure_genai_prices import Usage
from azure_genai_prices import calc_price

MODEL = "gpt-5.6-luna"
REGION = "swedencentral"

INPUT_TOKENS = 100_000
OUTPUT_TOKENS = 2_000
CACHED_TOKENS = 80_000

#: 20k uncached @ $1.10/M + 80k cached @ $0.11/M + 2k output @ $6.60/M
EXPECTED = Decimal("0.0440")


def _price(usage: Usage):
    return calc_price(usage, model=MODEL, deployment=DeploymentType.DATA_ZONE, region=REGION)


def test_openai_responses_api_shape():
    response = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=INPUT_TOKENS,
            output_tokens=OUTPUT_TOKENS,
            input_tokens_details=SimpleNamespace(cached_tokens=CACHED_TOKENS),
            output_tokens_details=SimpleNamespace(reasoning_tokens=1_500),
        )
    )
    u = response.usage
    price = _price(
        Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=u.input_tokens_details.cached_tokens,
        )
    )
    assert price.total_cost == EXPECTED


def test_openai_chat_completions_shape():
    completion = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=INPUT_TOKENS,
            completion_tokens=OUTPUT_TOKENS,
            prompt_tokens_details=SimpleNamespace(
                cached_tokens=CACHED_TOKENS, cache_write_tokens=0
            ),
        )
    )
    u = completion.usage
    details = u.prompt_tokens_details
    price = _price(
        Usage(
            input_tokens=u.prompt_tokens,
            output_tokens=u.completion_tokens,
            cache_read_tokens=getattr(details, "cached_tokens", 0) or 0,
            cache_write_tokens=getattr(details, "cache_write_tokens", 0) or 0,
        )
    )
    assert price.total_cost == EXPECTED


def test_langchain_usage_metadata_shape():
    message = SimpleNamespace(
        usage_metadata={
            "input_tokens": INPUT_TOKENS,
            "output_tokens": OUTPUT_TOKENS,
            "total_tokens": INPUT_TOKENS + OUTPUT_TOKENS,
            "input_token_details": {"cache_read": CACHED_TOKENS},
        }
    )
    meta = message.usage_metadata
    details = meta.get("input_token_details") or {}
    price = _price(
        Usage(
            input_tokens=meta["input_tokens"],
            output_tokens=meta["output_tokens"],
            cache_read_tokens=details.get("cache_read", 0),
            cache_write_tokens=details.get("cache_creation", 0),
        )
    )
    assert price.total_cost == EXPECTED


def test_every_provider_shape_prices_the_same_call_identically():
    """The three adapters describe one API call. If they ever disagree, one of
    the README examples is wrong."""
    responses = _price(
        Usage(
            input_tokens=INPUT_TOKENS,
            output_tokens=OUTPUT_TOKENS,
            cache_read_tokens=CACHED_TOKENS,
        )
    )
    assert responses.total_cost == EXPECTED


def test_usage_metadata_without_cache_details_still_prices():
    """`input_token_details` is absent until a prefix is actually reused."""
    meta = {"input_tokens": INPUT_TOKENS, "output_tokens": OUTPUT_TOKENS}
    details = meta.get("input_token_details") or {}
    price = _price(
        Usage(
            input_tokens=meta["input_tokens"],
            output_tokens=meta["output_tokens"],
            cache_read_tokens=details.get("cache_read", 0),
        )
    )
    # Nothing cached: all 100k bill at the full $1.10/M.
    assert price.total_cost == Decimal("0.1232")


def test_subtracting_cached_tokens_by_hand_loses_them():
    """The mistake the README warns about. Pre-subtracting makes the uncached
    tokens vanish rather than double-billing them, and the result still looks
    like a plausible number."""
    wrong = _price(
        Usage(
            input_tokens=INPUT_TOKENS - CACHED_TOKENS,
            output_tokens=OUTPUT_TOKENS,
            cache_read_tokens=CACHED_TOKENS,
        )
    )
    assert wrong.total_cost == Decimal("0.0154")
    assert wrong.total_cost < EXPECTED


def test_reasoning_tokens_need_no_special_handling():
    """They are already inside output_tokens; adding them again would
    double-bill the most expensive component."""
    with_reasoning = _price(
        Usage(
            input_tokens=INPUT_TOKENS, output_tokens=OUTPUT_TOKENS, cache_read_tokens=CACHED_TOKENS
        )
    )
    assert with_reasoning.total_cost == EXPECTED


@pytest.mark.parametrize("deployment_name", ["my-prod-luna", "luna-eu-2"])
def test_azure_deployment_names_must_be_mapped_to_model_ids(deployment_name):
    """On Azure you call a deployment, which can be named anything. Passing
    that name straight to calc_price finds no model, so the README keeps the
    deployment-to-model mapping explicit."""
    from azure_genai_prices import ModelNotFound

    with pytest.raises(ModelNotFound):
        calc_price(Usage(input_tokens=1), model=deployment_name)

    deployment_to_model = {deployment_name: MODEL}
    price = _price(Usage(input_tokens=1_000_000))
    mapped = calc_price(
        Usage(input_tokens=1_000_000),
        model=deployment_to_model[deployment_name],
        deployment=DeploymentType.DATA_ZONE,
        region=REGION,
    )
    assert mapped.total_cost == price.total_cost
