import pytest
from typer.testing import CliRunner

from ctlab.cli import app
from ctlab.config import env
from ctlab.execution.gateway import LiveTradingDisabled, assert_demo


def test_cli_info():
    result = CliRunner().invoke(app, ["info"])
    assert result.exit_code == 0
    assert "contract_size=100" in result.output


def test_order_layer_refuses_live(monkeypatch):
    monkeypatch.setenv("CTRADER_ENV", "live")
    env.cache_clear()
    try:
        with pytest.raises(LiveTradingDisabled):
            assert_demo()
    finally:
        env.cache_clear()


def test_order_layer_allows_demo(monkeypatch):
    monkeypatch.setenv("CTRADER_ENV", "demo")
    env.cache_clear()
    try:
        assert_demo()
    finally:
        env.cache_clear()
