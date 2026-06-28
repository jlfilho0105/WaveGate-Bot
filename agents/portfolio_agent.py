"""
AGENTE: PortfolioAgent
RESPONSABILIDADE: Rastreia posicoes abertas, equity, PnL e estado local.
"""

import json
import logging
import datetime
from pathlib import Path

from .signal_agent import TradeSignal

logger = logging.getLogger(__name__)


class PortfolioAgent:
    def __init__(self, config: dict):
        self.initial_equity = config.get("initial_equity_usdt", 10_000.0)
        self.equity = self.initial_equity
        self.open_positions: dict = {}
        self.closed_trades: list = []
        self._state_file = Path("data/portfolio_state.json")
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_state()

    def open_position(self, signal: TradeSignal, qty: float = 0.0, entry_id: str | None = None) -> None:
        self.open_positions[signal.symbol] = {
            "signal": signal,
            "open_time": datetime.datetime.now(datetime.UTC).isoformat(),
            "margin_usdt": signal.position_size_usdt,
            "qty": qty,
            "entry_id": entry_id,
            "live_protection": False,
        }
        self._save_state()
        logger.info(
            f"Posicao aberta: {signal.symbol} {signal.direction} | "
            f"entry={signal.entry_price:.6f} | margin={signal.position_size_usdt:.2f} USDT"
        )

    def protect_live_position(
        self,
        symbol: str,
        direction: str,
        qty: float,
        avg_price: float = 0.0,
        target_price: float = 0.0,
        stop_price: float = 0.0,
        side: str | None = None,
    ) -> None:
        signal = TradeSignal(
            symbol=symbol,
            direction=direction,
            entry_price=avg_price,
            target_price=target_price,
            stop_price=stop_price,
            leverage=1,
            risk_reward=0.0,
            conditions_met=["live_position_reconciled"],
            position_size_usdt=0.0,
        )
        self.open_positions[symbol] = {
            "signal": signal,
            "open_time": datetime.datetime.now(datetime.UTC).isoformat(),
            "margin_usdt": 0.0,
            "qty": qty,
            "entry_id": None,
            "live_protection": True,
            "side": side or direction,
        }
        self._save_state()
        logger.warning(
            f"Posicao LIVE ja existente protegida contra duplicidade: {symbol} {direction} qty={qty}"
        )

    def sync_live_equity(self, equity: float) -> None:
        if equity <= 0:
            return
        self.equity = equity
        self.initial_equity = equity
        self.closed_trades = []
        self._save_state()
        logger.info(f"Equity sincronizada com OKX: {equity:.2f} USDT")

    def get_open_qty(self, symbol: str) -> float:
        pos = self.open_positions.get(symbol) or {}
        try:
            return float(pos.get("qty") or 0)
        except (TypeError, ValueError):
            return 0.0

    def clear_open_positions(self) -> None:
        self.open_positions = {}
        self._save_state()

    def forget_open_position(self, symbol: str) -> None:
        if symbol in self.open_positions:
            self.open_positions.pop(symbol)
            self._save_state()
            logger.info(f"Posicao removida do estado local: {symbol}")

    def close_position(self, symbol: str, exit_price: float, outcome: str) -> float:
        if symbol not in self.open_positions:
            return 0.0

        pos = self.open_positions.pop(symbol)
        signal: TradeSignal = pos["signal"]
        margin = pos["margin_usdt"]

        if outcome == "WIN":
            pnl = margin * signal.leverage * (signal.target_pct / 100)
        elif outcome == "LOSS":
            pnl = -margin * signal.leverage * (signal.stop_pct / 100)
        else:
            if signal.direction == "SHORT":
                pnl_pct = (signal.entry_price - exit_price) / signal.entry_price
            else:
                pnl_pct = (exit_price - signal.entry_price) / signal.entry_price
            pnl = margin * signal.leverage * pnl_pct

        self.equity += pnl
        self.closed_trades.append({
            "symbol": symbol,
            "direction": signal.direction,
            "entry": signal.entry_price,
            "exit": round(exit_price, 8),
            "outcome": outcome,
            "pnl_usdt": round(pnl, 4),
            "equity": round(self.equity, 2),
            "conditions": ",".join(signal.conditions_met),
            "close_time": datetime.datetime.now(datetime.UTC).isoformat(),
        })
        self._save_state()
        logger.info(
            f"Posicao fechada: {symbol} {signal.direction} | {outcome} | "
            f"PnL={pnl:+.2f} USDT | Equity={self.equity:.2f}"
        )
        return pnl

    @property
    def total_pnl_usdt(self) -> float:
        return self.equity - self.initial_equity

    @property
    def total_pnl_pct(self) -> float:
        return self.total_pnl_usdt / self.initial_equity * 100

    @property
    def open_exposure_usdt(self) -> float:
        return sum(
            p["margin_usdt"] * p["signal"].leverage
            for p in self.open_positions.values()
        )

    @property
    def win_rate(self) -> float:
        wins = sum(1 for t in self.closed_trades if t["outcome"] == "WIN")
        total = len(self.closed_trades)
        return wins / total * 100 if total > 0 else 0.0

    def summary(self) -> str:
        n = len(self.closed_trades)
        wins = sum(1 for t in self.closed_trades if t["outcome"] == "WIN")
        return (
            f"Equity: {self.equity:.2f} USDT ({self.total_pnl_pct:+.2f}%)\n"
            f"Trades: {n} (W:{wins} L:{n-wins}) | WR: {self.win_rate:.1f}%\n"
            f"Abertas: {len(self.open_positions)} | Exposicao: {self.open_exposure_usdt:.2f} USDT"
        )

    def _save_state(self) -> None:
        try:
            data = {
                "equity": self.equity,
                "initial_equity": self.initial_equity,
                "closed_count": len(self.closed_trades),
                "open_symbols": list(self.open_positions.keys()),
                "open_positions": {
                    symbol: {
                        "direction": pos["signal"].direction,
                        "entry": pos["signal"].entry_price,
                        "target": pos["signal"].target_price,
                        "stop": pos["signal"].stop_price,
                        "leverage": pos["signal"].leverage,
                        "margin_usdt": pos.get("margin_usdt", 0.0),
                        "qty": pos.get("qty", 0.0),
                        "entry_id": pos.get("entry_id"),
                        "live_protection": pos.get("live_protection", False),
                        "side": pos.get("side") or pos["signal"].direction,
                        "open_time": pos.get("open_time"),
                    }
                    for symbol, pos in self.open_positions.items()
                },
                "saved_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
            self._state_file.write_text(json.dumps(data, indent=2, default=str))
        except Exception as e:
            logger.warning(f"Erro ao salvar estado: {e}")

    def _load_state(self) -> None:
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text())
                self.equity = data.get("equity", self.initial_equity)
                for symbol, pos in (data.get("open_positions") or {}).items():
                    signal = TradeSignal(
                        symbol=symbol,
                        direction=pos.get("direction", "LONG"),
                        entry_price=float(pos.get("entry") or 0),
                        target_price=float(pos.get("target") or 0),
                        stop_price=float(pos.get("stop") or 0),
                        leverage=int(pos.get("leverage") or 1),
                        risk_reward=0.0,
                        conditions_met=["restored_local_state"],
                        position_size_usdt=float(pos.get("margin_usdt") or 0),
                    )
                    self.open_positions[symbol] = {
                        "signal": signal,
                        "open_time": pos.get("open_time"),
                        "margin_usdt": float(pos.get("margin_usdt") or 0),
                        "qty": float(pos.get("qty") or 0),
                        "entry_id": pos.get("entry_id"),
                        "live_protection": bool(pos.get("live_protection", False)),
                        "side": pos.get("side") or pos.get("direction", "LONG"),
                    }
                logger.info(f"Estado carregado: equity={self.equity:.2f} USDT")
        except Exception as e:
            logger.warning(f"Erro ao carregar estado: {e}")
