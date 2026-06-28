import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.execution_agent import ExecutionAgent
from agents.portfolio_agent import PortfolioAgent
from agents.signal_agent import TradeSignal
from main import reconcile_live_positions


SYMBOL = "BTC-USDT-SWAP"


def make_signal():
    return TradeSignal(
        symbol=SYMBOL,
        direction="LONG",
        entry_price=100.0,
        target_price=100.6,
        stop_price=99.7,
        leverage=3,
        risk_reward=2.0,
        conditions_met=["fake_signal"],
        position_size_usdt=100.0,
    )


def make_portfolio(tmp_path):
    portfolio = PortfolioAgent({"initial_equity_usdt": 10_000.0})
    portfolio._state_file = tmp_path / "portfolio_state.json"
    portfolio.open_positions = {}
    portfolio.closed_trades = []
    portfolio.equity = 10_000.0
    portfolio.initial_equity = 10_000.0
    return portfolio


class FailingOrderExecution(ExecutionAgent):
    def __init__(self, order_response=None, exception=None):
        super().__init__({
            "paper_trade": False,
            "leverage": 3,
            "td_mode": "cross",
            "okx_api_key": "fake",
            "okx_api_secret": "fake",
            "okx_api_passphrase": "fake",
        })
        self.order_response = order_response
        self.exception = exception
        self.requests = []

    async def _contracts_for_notional(self, symbol, notional_usdt):
        return 1.0

    async def _request(self, method, path, payload=None):
        self.requests.append({"method": method, "path": path, "payload": payload})
        if path == "/api/v5/account/set-leverage":
            return {"code": "0", "data": [{"sCode": "0"}]}
        if self.exception:
            raise self.exception
        return self.order_response


class FakeReconcileExecution:
    def __init__(self, positions=None, exception=None):
        self.positions = positions
        self.exception = exception
        self.open_position_calls = []

    async def get_open_positions(self, symbols=None):
        if self.exception:
            raise self.exception
        return self.positions

    async def open_position(self, signal):
        self.open_position_calls.append(signal)
        return {"entry_id": "unsafe", "qty": 1.0}


def execute_entry_like_main(execution, portfolio, signal):
    try:
        result = asyncio.run(execution.open_position(signal))
    except Exception:
        return False
    if not result:
        return False
    portfolio.open_position(signal, qty=float(result.get("qty", 0) or 0), entry_id=result.get("entry_id"))
    return True


def test_api_failure_opening_order_does_not_create_local_position(tmp_path):
    portfolio = make_portfolio(tmp_path)
    execution = FailingOrderExecution(exception=RuntimeError("OKX unavailable"))

    opened = execute_entry_like_main(execution, portfolio, make_signal())

    assert opened is False
    assert portfolio.open_positions == {}


def test_api_failure_during_reconciliation_blocks_live_operation(tmp_path):
    portfolio = make_portfolio(tmp_path)
    execution = FakeReconcileExecution(exception=RuntimeError("OKX positions unavailable"))

    safe_to_operate = asyncio.run(reconcile_live_positions(execution, portfolio, [SYMBOL]))

    assert safe_to_operate is False
    assert execution.open_position_calls == []
    assert portfolio.open_positions == {}


def test_partial_incomplete_okx_order_response_is_safe_failure(tmp_path):
    portfolio = make_portfolio(tmp_path)
    execution = FailingOrderExecution(order_response={"code": "0"})

    opened = execute_entry_like_main(execution, portfolio, make_signal())

    assert opened is False
    assert portfolio.open_positions == {}


def test_api_timeout_does_not_send_duplicate_order_after_live_reconcile_failure(tmp_path):
    portfolio = make_portfolio(tmp_path)
    execution = FakeReconcileExecution(exception=TimeoutError("OKX timeout"))

    safe_to_operate = asyncio.run(reconcile_live_positions(execution, portfolio, [SYMBOL]))
    if safe_to_operate:
        asyncio.run(execution.open_position(make_signal()))

    assert safe_to_operate is False
    assert execution.open_position_calls == []
    assert portfolio.open_positions == {}


def test_incomplete_okx_reconciliation_response_leaves_no_inconsistent_position(tmp_path):
    portfolio = make_portfolio(tmp_path)
    execution = FakeReconcileExecution({SYMBOL: {"direction": "LONG", "avg_price": 100.0}})

    safe_to_operate = asyncio.run(reconcile_live_positions(execution, portfolio, [SYMBOL]))

    assert safe_to_operate is False
    assert portfolio.open_positions == {}
