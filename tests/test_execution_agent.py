import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.execution_agent import ExecutionAgent
from agents.signal_agent import TradeSignal


def okx_order_response(order_id="ord-1", s_code="0"):
    return {
        "code": "0",
        "data": [{
            "ordId": order_id,
            "sCode": s_code,
            "sMsg": "" if s_code == "0" else "rejected",
        }],
    }


def make_signal(direction="LONG"):
    entry = 100.0
    if direction == "SHORT":
        target = 99.0
        stop = 100.5
    else:
        target = 101.0
        stop = 99.5

    return TradeSignal(
        symbol="BTC-USDT-SWAP",
        direction=direction,
        entry_price=entry,
        target_price=target,
        stop_price=stop,
        leverage=3,
        risk_reward=2.0,
        conditions_met=["markov_gate", "wt_cross"],
        position_size_usdt=100.0,
    )


class FakeExecutionAgent(ExecutionAgent):
    def __init__(self, order_response=None, qty=2.5, order_exception=None):
        super().__init__({
            "paper_trade": False,
            "leverage": 3,
            "td_mode": "cross",
            "okx_api_key": "fake",
            "okx_api_secret": "fake",
            "okx_api_passphrase": "fake",
        })
        self.order_response = order_response or okx_order_response()
        self.qty = qty
        self.order_exception = order_exception
        self.requests = []

    async def _contracts_for_notional(self, symbol, notional_usdt):
        return self.qty

    async def _request(self, method, path, payload=None):
        self.requests.append({"method": method, "path": path, "payload": payload})
        if path == "/api/v5/account/set-leverage":
            return {"code": "0", "data": [{"sCode": "0"}]}
        if self.order_exception:
            raise self.order_exception
        return self.order_response

    @property
    def order_payload(self):
        return next(
            req["payload"]
            for req in self.requests
            if req["path"] == "/api/v5/trade/order"
        )


def test_long_order_success_creates_valid_response():
    agent = FakeExecutionAgent(order_response=okx_order_response("ord-long"), qty=3.0)

    result = asyncio.run(agent.open_position(make_signal("LONG")))

    assert result == {"entry_id": "ord-long", "qty": 3.0}
    assert agent.order_payload["side"] == "buy"
    assert agent.order_payload["instId"] == "BTC-USDT-SWAP"
    assert agent.order_payload["ordType"] == "market"
    assert agent.order_payload["sz"] == "3.0"


def test_short_order_success_creates_valid_response():
    agent = FakeExecutionAgent(order_response=okx_order_response("ord-short"), qty=1.25)

    result = asyncio.run(agent.open_position(make_signal("SHORT")))

    assert result == {"entry_id": "ord-short", "qty": 1.25}
    assert agent.order_payload["side"] == "sell"
    assert agent.order_payload["instId"] == "BTC-USDT-SWAP"


def test_okx_response_with_non_zero_code_is_failure():
    agent = FakeExecutionAgent(order_response={"code": "51000", "msg": "bad request", "data": []})

    result = asyncio.run(agent.open_position(make_signal("LONG")))

    assert result == {}


def test_okx_response_with_non_zero_scode_is_failure():
    agent = FakeExecutionAgent(order_response=okx_order_response("ord-rejected", s_code="51008"))

    result = asyncio.run(agent.open_position(make_signal("LONG")))

    assert result == {}


def test_api_exception_does_not_create_local_position_response():
    agent = FakeExecutionAgent(order_exception=RuntimeError("okx unavailable"))

    with pytest.raises(RuntimeError, match="okx unavailable"):
        asyncio.run(agent.open_position(make_signal("LONG")))


def test_attach_algo_orders_contains_tp_and_sl_required_by_strategy():
    signal = make_signal("LONG")
    agent = FakeExecutionAgent(order_response=okx_order_response("ord-tpsl"))

    asyncio.run(agent.open_position(signal))

    attach_algo_orders = agent.order_payload["attachAlgoOrds"]
    assert attach_algo_orders == [{
        "tpTriggerPx": str(signal.target_price),
        "tpOrdPx": "-1",
        "slTriggerPx": str(signal.stop_price),
        "slOrdPx": "-1",
    }]


def test_swap_order_uses_net_mode_payload_without_pos_side():
    """Current SWAP behavior is OKX net mode: no posSide is sent in entry payloads."""
    agent = FakeExecutionAgent(order_response=okx_order_response("ord-net-mode"))

    asyncio.run(agent.open_position(make_signal("LONG")))

    assert agent.order_payload["instId"].endswith("-SWAP")
    assert "posSide" not in agent.order_payload
