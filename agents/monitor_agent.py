"""
AGENTE: MonitorAgent
RESPONSABILIDADE: Acompanha posicoes LONG/SHORT, alvo, stop e timeout.
"""

import logging
from datetime import datetime, timedelta
from typing import Callable, Dict

from .signal_agent import TradeSignal

logger = logging.getLogger(__name__)


class MonitorAgent:
    def __init__(self, config: dict, portfolio=None):
        self.update_threshold_pct = config.get("monitor_update_pct", 25)
        self.max_duration_min = config.get("max_duration_min", 120)
        self._portfolio = portfolio
        self._watches: Dict[str, dict] = {}
        self._callbacks: Dict[str, Callable] = {}

    def on_event(self, event_type: str, callback: Callable) -> None:
        self._callbacks[event_type] = callback

    def start_monitoring(self, signal: TradeSignal) -> None:
        self._watches[signal.symbol] = {
            "signal": signal,
            "start_time": datetime.utcnow(),
            "last_notified_pct": 0,
        }
        logger.info(
            f"Monitorando {signal.symbol} {signal.direction} | "
            f"alvo={signal.target_price:.6f} stop={signal.stop_price:.6f}"
        )

    def stop_monitoring(self, symbol: str) -> None:
        if symbol in self._watches:
            del self._watches[symbol]
            logger.info(f"Monitoramento encerrado: {symbol}")

    def update_price(self, symbol: str, current_price: float) -> None:
        if symbol not in self._watches:
            return

        watch = self._watches[symbol]
        signal: TradeSignal = watch["signal"]
        elapsed = datetime.utcnow() - watch["start_time"]

        if elapsed > timedelta(minutes=self.max_duration_min):
            pnl = self._portfolio.close_position(symbol, current_price, "TIMEOUT") if self._portfolio else 0.0
            self._emit("on_timeout", signal, current_price, pnl)
            self.stop_monitoring(symbol)
            return

        if self._hit_stop(signal, current_price):
            pnl = self._portfolio.close_position(symbol, signal.stop_price, "LOSS") if self._portfolio else 0.0
            self._emit("on_stop", signal, pnl)
            self.stop_monitoring(symbol)
            return

        if self._hit_target(signal, current_price):
            pnl = self._portfolio.close_position(symbol, signal.target_price, "WIN") if self._portfolio else 0.0
            self._emit("on_target", signal, pnl)
            self.stop_monitoring(symbol)
            return

        pct_done = self._progress_pct(signal, current_price)
        last_notified = watch["last_notified_pct"]
        threshold = self.update_threshold_pct
        if pct_done >= last_notified + threshold:
            watch["last_notified_pct"] = int(pct_done // threshold) * threshold
            self._emit("on_progress", signal, pct_done, current_price)

    def _hit_stop(self, signal: TradeSignal, price: float) -> bool:
        if signal.direction == "SHORT":
            return price >= signal.stop_price
        return price <= signal.stop_price

    def _hit_target(self, signal: TradeSignal, price: float) -> bool:
        if signal.direction == "SHORT":
            return price <= signal.target_price
        return price >= signal.target_price

    def _progress_pct(self, signal: TradeSignal, price: float) -> float:
        total_range = abs(signal.target_price - signal.entry_price)
        if total_range <= 0:
            return 0.0
        if signal.direction == "SHORT":
            return (signal.entry_price - price) / total_range * 100
        return (price - signal.entry_price) / total_range * 100

    def _emit(self, event: str, *args) -> None:
        if event in self._callbacks:
            try:
                self._callbacks[event](*args)
            except Exception as e:
                logger.error(f"Erro no callback {event}: {e}")

    @property
    def active_count(self) -> int:
        return len(self._watches)

    def active_symbols(self) -> list:
        return list(self._watches.keys())
