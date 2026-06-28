import asyncio
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.execution_agent import ExecutionAgent
from agents.portfolio_agent import PortfolioAgent
from agents.risk_agent import RiskAgent
from agents.signal_agent import SignalAgent


SYMBOL = "BTC-USDT-SWAP"


class FakeExecutionAgent(ExecutionAgent):
    def __init__(self, qty=1.5):
        super().__init__({
            "paper_trade": False,
            "leverage": 3,
            "td_mode": "cross",
            "okx_api_key": "fake",
            "okx_api_secret": "fake",
            "okx_api_passphrase": "fake",
        })
        self.qty = qty
        self.requests = []

    async def _contracts_for_notional(self, symbol, notional_usdt):
        return self.qty

    async def _request(self, method, path, payload=None):
        self.requests.append({"method": method, "path": path, "payload": payload})
        if path == "/api/v5/account/set-leverage":
            return {"code": "0", "data": [{"sCode": "0"}]}
        return {"code": "0", "data": [{"ordId": f"ord-{payload['side']}", "sCode": "0"}]}

    @property
    def order_payload(self):
        return next(
            req["payload"]
            for req in self.requests
            if req["path"] == "/api/v5/trade/order"
        )


def config():
    return {
        "paper_trade": False,
        "initial_equity_usdt": 10_000.0,
        "leverage": 3,
        "h1_enabled": False,
        "target_pct": 0.006,
        "stop_pct": 0.003,
        "min_rr": 1.8,
        "min_conditions": 4,
        "risk_per_trade_pct": 1.0,
        "max_open_positions": 3,
    }


def make_portfolio(tmp_path):
    portfolio = PortfolioAgent(config())
    portfolio._state_file = tmp_path / "portfolio_state.json"
    portfolio.open_positions = {}
    portfolio.closed_trades = []
    portfolio.equity = 10_000.0
    portfolio.initial_equity = 10_000.0
    return portfolio


def make_strategy_frame(direction):
    rows = []
    for idx in range(200):
        if direction == "LONG":
            row = {
                "open": 99.8,
                "high": 100.4,
                "low": 99.6,
                "close": 100.0,
                "volume": 100.0,
                "volume_ma": 100.0,
                "ema_9": 101.0,
                "ema_21": 100.5,
                "ema_55": 99.0,
                "macd_hist": 0.1,
                "wt_cross_up": False,
                "wt_cross_down": False,
                "wt1": -35.0,
            }
        else:
            row = {
                "open": 100.2,
                "high": 100.4,
                "low": 99.6,
                "close": 100.0,
                "volume": 100.0,
                "volume_ma": 100.0,
                "ema_9": 98.0,
                "ema_21": 99.0,
                "ema_55": 101.0,
                "macd_hist": -0.1,
                "wt_cross_up": False,
                "wt_cross_down": False,
                "wt1": 65.0,
            }
        rows.append(row)

    df = pd.DataFrame(rows, index=pd.date_range("2026-01-01", periods=200, freq="5min", tz="UTC"))
    if direction == "LONG":
        df.iloc[-2, df.columns.get_loc("macd_hist")] = 0.05
        df.iloc[-1, df.columns.get_loc("wt_cross_up")] = True
    else:
        df.iloc[-2, df.columns.get_loc("macd_hist")] = -0.05
        df.iloc[-1, df.columns.get_loc("wt_cross_down")] = True
    return df


def run_signal_to_fake_execution(tmp_path, regime, frame_direction):
    cfg = config()
    signal_agent = SignalAgent(cfg)
    risk = RiskAgent(cfg)
    portfolio = make_portfolio(tmp_path)
    execution = FakeExecutionAgent(qty=2.0)

    trade_signal = signal_agent.evaluate(
        SYMBOL,
        make_strategy_frame(frame_direction),
        pd.Series([100.0] * 40),
        regime=regime,
    )
    if not trade_signal:
        return None, execution, portfolio

    assert risk.can_open(SYMBOL, portfolio)
    trade_signal.position_size_usdt = risk.size_position(trade_signal, portfolio.equity)
    result = asyncio.run(execution.open_position(trade_signal))
    if result:
        portfolio.open_position(
            trade_signal,
            qty=float(result.get("qty", 0) or 0),
            entry_id=result.get("entry_id"),
        )
    return trade_signal, execution, portfolio


def test_bull_regime_generates_valid_long_and_sends_buy_side(tmp_path):
    trade_signal, execution, portfolio = run_signal_to_fake_execution(tmp_path, "Bull", "LONG")

    assert trade_signal is not None
    assert trade_signal.direction == "LONG"
    assert execution.order_payload["side"] == "buy"
    assert portfolio.open_positions[SYMBOL]["signal"].direction == "LONG"


def test_bear_regime_generates_valid_short_and_sends_sell_side(tmp_path):
    trade_signal, execution, portfolio = run_signal_to_fake_execution(tmp_path, "Bear", "SHORT")

    assert trade_signal is not None
    assert trade_signal.direction == "SHORT"
    assert execution.order_payload["side"] == "sell"
    assert portfolio.open_positions[SYMBOL]["signal"].direction == "SHORT"


@pytest.mark.parametrize(
    ("regime", "frame_direction", "expected_side", "expected_target", "expected_stop"),
    [
        ("Bull", "LONG", "buy", 100.6, 99.7),
        ("Bear", "SHORT", "sell", 99.4, 100.3),
    ],
)
def test_tp_and_sl_are_calculated_and_preserved_in_execution_payload(
    tmp_path,
    regime,
    frame_direction,
    expected_side,
    expected_target,
    expected_stop,
):
    trade_signal, execution, portfolio = run_signal_to_fake_execution(tmp_path, regime, frame_direction)

    assert trade_signal.target_price == pytest.approx(expected_target)
    assert trade_signal.stop_price == pytest.approx(expected_stop)
    assert execution.order_payload["side"] == expected_side
    assert execution.order_payload["attachAlgoOrds"] == [{
        "tpTriggerPx": str(trade_signal.target_price),
        "tpOrdPx": "-1",
        "slTriggerPx": str(trade_signal.stop_price),
        "slOrdPx": "-1",
    }]
    saved_signal = portfolio.open_positions[SYMBOL]["signal"]
    assert saved_signal.target_price == trade_signal.target_price
    assert saved_signal.stop_price == trade_signal.stop_price


def test_sideways_neutral_signal_does_not_send_order(tmp_path):
    trade_signal, execution, portfolio = run_signal_to_fake_execution(tmp_path, "Sideways", "LONG")

    assert trade_signal is None
    assert execution.requests == []
    assert portfolio.open_positions == {}
