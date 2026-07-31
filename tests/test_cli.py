"""CLI smoke tests — it is a public surface, so it has to keep working."""

from __future__ import annotations

import pytest

from azure_genai_prices import reset
from azure_genai_prices.cli import main


@pytest.fixture(autouse=True)
def _clean():
    reset()
    yield
    reset()


def test_price_command(capsys):
    assert main(["price", "gpt-5.6-luna", "--input-tokens", "1000000"]) == 0
    out = capsys.readouterr().out
    assert "gpt-5.6-luna" in out
    assert "total" in out


def test_price_command_honours_data_zone(capsys):
    main(["price", "gpt-5.6-luna", "--input-tokens", "1000000", "--deployment", "global"])
    global_out = capsys.readouterr().out
    main(["price", "gpt-5.6-luna", "--input-tokens", "1000000", "--deployment", "data-zone"])
    dz_out = capsys.readouterr().out
    assert global_out != dz_out


def test_models_command(capsys):
    assert main(["models", "--filter", "gpt-5.6"]) == 0
    out = capsys.readouterr().out
    assert "gpt-5.6-luna" in out


def test_coverage_command_exits_zero_when_everything_parsed(capsys):
    assert main(["coverage"]) == 0
    assert "failed:  0" in capsys.readouterr().out
