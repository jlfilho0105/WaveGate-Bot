"""
AGENTE: SignalAgent
RESPONSABILIDADE: Gera sinais LONG combinando WaveTrend M5 + confirmações técnicas.

Lógica: O gate Markov (long-only) já foi validado em main.py antes de chamar evaluate().
Aqui avaliamos apenas a qualidade técnica do sinal M5.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import pandas as pd

logger = logging.getLogger(__name__)

# Rótulos legíveis para o Telegram
CONDITION_LABELS = {
    "markov_gate":   "🧠 Markov Bull",
    "wt_cross_up":   "🌊 WaveSignal",
    "above_ema55":   "📈 Acima EMA55",
    "ema_aligned":   "⚡ EMA alinhada",
    "volume_spike":  "📊 Volume spike",
    "macd_up":       "📉 MACD subindo",
    "bullish_body":  "🟢 Corpo bullish",
}


@dataclass
class TradeSignal:
    symbol: str
    direction: str
    entry_price: float
    target_price: float
    stop_price: float
    leverage: int
    risk_reward: float
    conditions_met: list
    position_size_usdt: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)
    wt1_value: float = 0.0

    @property
    def target_pct(self) -> float:
        return ((self.target_price - self.entry_price) / self.entry_price) * 100

    @property
    def stop_pct(self) -> float:
        return ((self.entry_price - self.stop_price) / self.entry_price) * 100


class SignalAgent:
    def __init__(self, config: dict):
        self.leverage        = config.get("leverage",        3)
        self.min_rr          = config.get("min_rr",        2.5)
        self.volume_factor   = config.get("volume_factor",  1.5)
        self.target_pct      = config.get("target_pct",  0.015)
        self.stop_pct        = config.get("stop_pct",    0.005)
        self.body_ratio_min  = config.get("body_ratio_min", 0.50)
        self.min_conditions  = config.get("min_conditions",    4)
        self._active: dict   = {}

    def evaluate(self, symbol: str, df: pd.DataFrame, df_daily: pd.Series) -> Optional[TradeSignal]:
        if symbol in self._active:
            return None
        if len(df) < 80:
            return None
        return self._check_long(symbol, df)

    def _check_long(self, symbol: str, df: pd.DataFrame) -> Optional[TradeSignal]:
        last = df.iloc[-1]
        prev = df.iloc[-2]

        entry  = float(last["close"])
        ema9   = float(last.get("ema_9",  0))
        ema21  = float(last.get("ema_21", 0))
        ema55  = float(last.get("ema_55", 0))
        vol    = float(last.get("volume",    0))
        vol_ma = float(last.get("volume_ma", 1) or 1)
        wt1    = float(last.get("wt1", 0))

        if ema21 <= 0 or ema55 <= 0:
            return None

        conditions = ["markov_gate"]  # já validado pelo main antes de chamar evaluate()

        # OBRIGATÓRIO: WaveTrend crossover de sobrevenda
        if not last.get("wt_cross_up", False):
            return None
        conditions.append("wt_cross_up")

        # Preço acima da EMA55 (uptrend macro M5)
        if entry > ema55:
            conditions.append("above_ema55")

        # Alinhamento EMA bullish
        if ema9 > ema21:
            conditions.append("ema_aligned")

        # Volume spike
        if vol > vol_ma * self.volume_factor:
            conditions.append("volume_spike")

        # MACD histogram subindo
        if last.get("macd_hist", 0) > prev.get("macd_hist", 0):
            conditions.append("macd_up")

        # Candle com corpo sólido e bullish
        candle_range = last["high"] - last["low"]
        body = last["close"] - last["open"]
        if body > 0 and candle_range > 0 and body / candle_range >= self.body_ratio_min:
            conditions.append("bullish_body")

        if len(conditions) < self.min_conditions:
            return None

        stop   = entry * (1 - self.stop_pct)
        target = entry * (1 + self.target_pct)
        if stop >= entry:
            return None

        rr = (target - entry) / (entry - stop)
        if rr < self.min_rr:
            return None

        sig = TradeSignal(
            symbol         = symbol,
            direction      = "LONG",
            entry_price    = round(entry, 8),
            target_price   = round(target, 8),
            stop_price     = round(stop, 8),
            leverage       = self.leverage,
            risk_reward    = round(rr, 2),
            conditions_met = conditions,
            wt1_value      = round(wt1, 2),
        )
        self._active[symbol] = sig
        logger.info(
            f"SINAL LONG | {symbol} | entry={entry:.4f} "
            f"alvo={target:.4f}(+{self.target_pct*100:.1f}%) "
            f"stop={stop:.4f}(-{self.stop_pct*100:.1f}%) "
            f"R/R={rr:.2f} WT1={wt1:.1f} | {conditions}"
        )
        return sig

    def clear_signal(self, symbol: str) -> None:
        self._active.pop(symbol, None)
