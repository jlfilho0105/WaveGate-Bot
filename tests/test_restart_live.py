import asyncio
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as bot_main
from agents.portfolio_agent import PortfolioAgent
from agents.signal_agent import TradeSignal


class RestartFlowComplete(Exception):
    pass


def okx_live_position():
    return {
        "direction": "LONG",
        "qty": 2.75,
        "avg_price": 62_500.5,
        "target_price": 63_250.25,
        "stop_price": 61_900.75,
        "side": "buy",
    }


def base_config():
    return {
        "symbols": ["BTC-USDT-SWAP"],
        "paper_trade": False,
        "initial_equity_usdt": 10_000.0,
        "monitor_update_pct": 25,
        "max_duration_min": 120,
        "okx_api_key": "fake-key",
        "okx_api_secret": "fake-secret",
        "okx_api_passphrase": "fake-passphrase",
        "telegram_token": "",
        "telegram_chat_id": "",
        "trade_sideways": True,
        "leverage": 3,
    }


class FakeDataAgent:
    instances = []

    def __init__(self, config):
        self.config = config
        self.callbacks = {}
        self.start_calls = 0
        FakeDataAgent.instances.append(self)

    async def get_daily_close(self, symbol, years=3):
        return pd.Series([100.0] * 40)

    async def subscribe(self, symbol, callback):
        self.callbacks[symbol] = callback

    async def start(self):
        self.start_calls += 1
        df = pd.DataFrame(
            [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10.0}],
            index=pd.date_range("2026-01-01", periods=1, freq="5min", tz="UTC"),
        )
        await self.callbacks["BTC-USDT-SWAP"]("BTC-USDT-SWAP", df)
        raise RestartFlowComplete


class FakeMarkovAgent:
    def __init__(self, config):
        pass

    def update_cache(self, symbol, close_daily):
        pass

    def load_cache(self, symbol):
        return pd.Series([100.0] * 40)

    def get_regime(self, symbol, close_daily):
        return "Bull"


class PassthroughAgent:
    def __init__(self, config):
        pass

    def calculate(self, df):
        return df


class FakeSignalAgent:
    instances = []

    def __init__(self, config):
        self.evaluate_calls = 0
        self.clear_calls = []
        FakeSignalAgent.instances.append(self)

    def evaluate(self, symbol, df_wave, df_daily, regime="Sideways"):
        self.evaluate_calls += 1
        return TradeSignal(
            symbol=symbol,
            direction="LONG",
            entry_price=100.0,
            target_price=101.0,
            stop_price=99.5,
            leverage=3,
            risk_reward=2.0,
            conditions_met=["fake_signal"],
            position_size_usdt=100.0,
        )

    def clear_signal(self, symbol):
        self.clear_calls.append(symbol)


class FakeRiskAgent:
    def __init__(self, config):
        pass

    def can_open(self, symbol, portfolio):
        return True

    def size_position(self, signal, equity):
        return 100.0

    def register_result(self, pnl):
        pass


class FakeExecutionAgent:
    instances = []

    def __init__(self, config, positions=None, failure=None):
        self.config = config
        self.positions = positions if positions is not None else {"BTC-USDT-SWAP": okx_live_position()}
        self.failure = failure
        self.get_positions_calls = []
        self.open_position_calls = []
        FakeExecutionAgent.instances.append(self)

    async def get_account_equity_usdt(self):
        return 10_000.0

    async def get_open_positions(self, symbols=None):
        self.get_positions_calls.append(symbols)
        if self.failure:
            raise self.failure
        return self.positions

    async def open_position(self, signal):
        self.open_position_calls.append(signal)
        return {"entry_id": "unexpected", "qty": 1.0}

    async def close_position(self, signal, qty):
        return {}


@pytest.fixture
def restart_harness(monkeypatch, tmp_path):
    FakeDataAgent.instances = []
    FakeSignalAgent.instances = []
    FakeExecutionAgent.instances = []

    config = base_config()
    portfolio = PortfolioAgent(config)
    portfolio._state_file = tmp_path / "portfolio_state.json"
    portfolio.open_positions = {}
    portfolio.closed_trades = []
    portfolio.equity = 10_000.0
    portfolio.initial_equity = 10_000.0

    monkeypatch.setattr(bot_main, "PAPER_TRADE", False)
    monkeypatch.setattr(bot_main, "load_config", lambda: config)
    monkeypatch.setattr(bot_main, "DataAgent", FakeDataAgent)
    monkeypatch.setattr(bot_main, "MarkovAgent", FakeMarkovAgent)
    monkeypatch.setattr(bot_main, "WaveAgent", PassthroughAgent)
    monkeypatch.setattr(bot_main, "IndicatorAgent", PassthroughAgent)
    monkeypatch.setattr(bot_main, "SignalAgent", FakeSignalAgent)
    monkeypatch.setattr(bot_main, "RiskAgent", FakeRiskAgent)
    monkeypatch.setattr(bot_main, "PortfolioAgent", lambda cfg: portfolio)

    return config, portfolio, monkeypatch


def test_full_live_restart_reconciles_and_blocks_duplicate_entry(restart_harness):
    _config, portfolio, monkeypatch = restart_harness
    monkeypatch.setattr(bot_main, "ExecutionAgent", FakeExecutionAgent)

    with pytest.raises(RestartFlowComplete):
        asyncio.run(bot_main.main())

    execution = FakeExecutionAgent.instances[0]
    signal = FakeSignalAgent.instances[0]
    position = portfolio.open_positions["BTC-USDT-SWAP"]
    reconciled_signal = position["signal"]

    assert execution.config["paper_trade"] is False
    assert "BTC-USDT-SWAP" in portfolio.open_positions
    assert position["live_protection"] is True
    assert execution.open_position_calls == []
    assert signal.evaluate_calls == 0
    assert position["qty"] == 2.75
    assert position["side"] == "buy"
    assert reconciled_signal.entry_price == 62_500.5
    assert reconciled_signal.target_price == 63_250.25
    assert reconciled_signal.stop_price == 61_900.75


def test_live_restart_reconcile_failure_does_not_operate_unsafely(restart_harness):
    _config, portfolio, monkeypatch = restart_harness

    class FailingExecutionAgent(FakeExecutionAgent):
        def __init__(self, config):
            super().__init__(config, failure=RuntimeError("OKX restart failure"))

    monkeypatch.setattr(bot_main, "ExecutionAgent", FailingExecutionAgent)

    asyncio.run(bot_main.main())

    execution = FailingExecutionAgent.instances[0]
    assert portfolio.open_positions == {}
    assert FakeDataAgent.instances[0].callbacks == {}
    assert FakeDataAgent.instances[0].start_calls == 0
    assert execution.open_position_calls == []
