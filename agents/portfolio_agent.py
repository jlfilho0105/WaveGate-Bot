"""
AGENTE: PortfolioAgent
RESPONSABILIDADE: Rastreia posicoes abertas, equity, PnL e estado local.
"""

import json
import logging
from datetime import datetime
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

    def open_position(self, signal: TradeSignal) -> None:
        self.open_positions[signal.symbol] = {
            "signal": signal,
            "open_time": datetime.utcnow().isoformat(),
            "margin_usdt": signal.position_size_usdt,
        }
        self._save_state()
        logger.info(
            f"Posicao aberta: {signal.symbol} {signal.direction} | "
            f"entry={signal.entry_price:.6f} | margin={signal.position_size_usdt:.2f} USDT"
        )

    def sync_live_equity(self, equity: float) -> None:
        if equity <= 0:
            return
        self.equity = equity
        self.initial_equity = equity
        self.closed_trades = []
        self._save_state()
        logger.info(f"Equity sincronizada com OKX: {equity:.2f} USDT")

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
            "close_time": datetime.utcnow().isoformat(),
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
                "saved_at": datetime.utcnow().isoformat(),
            }
            self._state_file.write_text(json.dumps(data, indent=2, default=str))
        except Exception as e:
            logger.warning(f"Erro ao salvar estado: {e}")

    def _load_state(self) -> None:
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text())
                self.equity = data.get("equity", self.initial_equity)
                logger.info(f"Estado carregado: equity={self.equity:.2f} USDT")
        except Exception as e:
            logger.warning(f"Erro ao carregar estado: {e}")
