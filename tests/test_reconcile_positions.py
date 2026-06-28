import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.portfolio_agent import PortfolioAgent
from main import reconcile_live_positions


class FakeExecution:
    def __init__(self, positions=None, exception=None):
        self.positions = positions
        self.exception = exception
        self.calls = []

    async def get_open_positions(self, symbols=None):
        self.calls.append(symbols)
        if self.exception:
            raise self.exception
        return self.positions


def make_portfolio(tmp_path):
    portfolio = PortfolioAgent({"initial_equity_usdt": 10_000.0})
    portfolio._state_file = tmp_path / "portfolio_state.json"
    portfolio.open_positions = {}
    portfolio.closed_trades = []
    portfolio.equity = 10_000.0
    portfolio.initial_equity = 10_000.0
    return portfolio


def live_position(
    direction="LONG",
    qty=1.25,
    avg_price=65_000.0,
    target_price=66_000.0,
    stop_price=64_000.0,
    side="buy",
):
    return {
        "direction": direction,
        "qty": qty,
        "avg_price": avg_price,
        "target_price": target_price,
        "stop_price": stop_price,
        "side": side,
    }


def test_okx_open_position_fills_portfolio_open_positions(tmp_path):
    portfolio = make_portfolio(tmp_path)
    execution = FakeExecution({
        "BTC-USDT-SWAP": live_position(),
    })

    ok = asyncio.run(reconcile_live_positions(execution, portfolio, ["BTC-USDT-SWAP"]))

    assert ok is True
    assert "BTC-USDT-SWAP" in portfolio.open_positions
    assert portfolio.open_positions["BTC-USDT-SWAP"]["live_protection"] is True


def test_reconciled_btc_position_blocks_duplicate_entry_for_same_symbol(tmp_path):
    portfolio = make_portfolio(tmp_path)
    execution = FakeExecution({
        "BTC-USDT-SWAP": live_position(),
    })

    asyncio.run(reconcile_live_positions(execution, portfolio, ["BTC-USDT-SWAP"]))

    local_position = portfolio.open_positions.get("BTC-USDT-SWAP")
    should_attempt_new_entry = local_position is None
    assert should_attempt_new_entry is False


def test_empty_okx_position_list_keeps_portfolio_without_open_positions(tmp_path):
    portfolio = make_portfolio(tmp_path)
    execution = FakeExecution({})

    ok = asyncio.run(reconcile_live_positions(execution, portfolio, ["BTC-USDT-SWAP"]))

    assert ok is True
    assert portfolio.open_positions == {}


def test_okx_api_failure_is_safe_and_blocks_live_startup(tmp_path, caplog):
    portfolio = make_portfolio(tmp_path)
    execution = FakeExecution(exception=RuntimeError("OKX unavailable"))

    ok = asyncio.run(reconcile_live_positions(execution, portfolio, ["BTC-USDT-SWAP"]))

    assert ok is False
    assert portfolio.open_positions == {}
    assert "abortando LIVE" in caplog.text


def test_reconcile_preserves_long_and_short_sides(tmp_path):
    portfolio = make_portfolio(tmp_path)
    execution = FakeExecution({
        "BTC-USDT-SWAP": live_position(direction="LONG", side="buy"),
        "ETH-USDT-SWAP": live_position(
            direction="SHORT",
            qty=3.0,
            avg_price=3_500.0,
            target_price=3_400.0,
            stop_price=3_550.0,
            side="sell",
        ),
    })

    ok = asyncio.run(reconcile_live_positions(execution, portfolio, ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]))

    assert ok is True
    assert portfolio.open_positions["BTC-USDT-SWAP"]["signal"].direction == "LONG"
    assert portfolio.open_positions["BTC-USDT-SWAP"]["side"] == "buy"
    assert portfolio.open_positions["ETH-USDT-SWAP"]["signal"].direction == "SHORT"
    assert portfolio.open_positions["ETH-USDT-SWAP"]["side"] == "sell"


def test_reconcile_preserves_available_qty_side_entry_tp_sl_and_symbol(tmp_path):
    portfolio = make_portfolio(tmp_path)
    execution = FakeExecution({
        "BTC-USDT-SWAP": live_position(
            direction="LONG",
            qty=2.75,
            avg_price=62_500.5,
            target_price=63_250.25,
            stop_price=61_900.75,
            side="buy",
        ),
    })

    ok = asyncio.run(reconcile_live_positions(execution, portfolio, ["BTC-USDT-SWAP"]))

    assert ok is True
    position = portfolio.open_positions["BTC-USDT-SWAP"]
    signal = position["signal"]
    assert signal.symbol == "BTC-USDT-SWAP"
    assert position["qty"] == 2.75
    assert position["side"] == "buy"
    assert signal.entry_price == 62_500.5
    assert signal.target_price == 63_250.25
    assert signal.stop_price == 61_900.75
