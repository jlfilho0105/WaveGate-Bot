"""
AGENTE: SignalAgent
RESPONSABILIDADE: Gera sinais LONG/SHORT combinando Markov diario, H1 e WaveTrend M5.
"""

import logging
import datetime
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

CONDITION_LABELS = {
    "markov_bull": "Markov Bull",
    "markov_bear": "Markov Bear",
    "h1_trend": "H1 tendencia",
    "wt_cross_up": "WaveTrend alta",
    "wt_cross_down": "WaveTrend baixa",
    "above_ema55": "Acima EMA55",
    "below_ema55": "Abaixo EMA55",
    "ema_aligned": "EMA alinhada",
    "ema_aligned_down": "EMA alinhada baixa",
    "volume_spike": "Volume spike",
    "macd_up": "MACD subindo",
    "macd_down": "MACD caindo",
    "bullish_body": "Corpo bullish",
    "bearish_body": "Corpo bearish",
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
    timestamp: datetime.datetime = field(default_factory=lambda: datetime.datetime.now(datetime.UTC))
    wt1_value: float = 0.0

    @property
    def target_pct(self) -> float:
        if self.direction == "SHORT":
            return ((self.entry_price - self.target_price) / self.entry_price) * 100
        return ((self.target_price - self.entry_price) / self.entry_price) * 100

    @property
    def stop_pct(self) -> float:
        if self.direction == "SHORT":
            return ((self.stop_price - self.entry_price) / self.entry_price) * 100
        return ((self.entry_price - self.stop_price) / self.entry_price) * 100


class SignalAgent:
    def __init__(self, config: dict):
        self.leverage = config.get("leverage", 3)
        self.min_rr = config.get("min_rr", 1.8)
        self.volume_factor = config.get("volume_factor", 1.5)
        self.target_pct = config.get("target_pct", 0.006)
        self.stop_pct = config.get("stop_pct", 0.003)
        self.body_ratio_min = config.get("body_ratio_min", 0.50)
        self.min_conditions = config.get("min_conditions", 4)
        self.allow_long = config.get("allow_long", True)
        self.allow_short = config.get("allow_short", True)
        self.h1_enabled = config.get("h1_enabled", True)
        self.h1_ema_fast = config.get("h1_ema_fast", 21)
        self.h1_ema_slow = config.get("h1_ema_slow", 55)
        self.h1_macd_confirm = config.get("h1_macd_confirm", True)
        self._active: dict = {}

    def evaluate(self, symbol: str, df: pd.DataFrame, df_daily: pd.Series, regime: str = "Sideways") -> Optional[TradeSignal]:
        if symbol in self._active:
            return None
        if len(df) < 200:
            return None
        if regime == "Bull" and self.allow_long:
            return self._check_long(symbol, df)
        if regime == "Bear" and self.allow_short:
            return self._check_short(symbol, df)
        return None

    def _check_long(self, symbol: str, df: pd.DataFrame) -> Optional[TradeSignal]:
        last = df.iloc[-1]
        prev = df.iloc[-2]
        entry = float(last["close"])

        if self.h1_enabled and not self._h1_allows(df, "LONG"):
            return None
        if not bool(last.get("wt_cross_up", False)):
            return None

        conditions = ["markov_bull", "h1_trend", "wt_cross_up"] if self.h1_enabled else ["markov_bull", "wt_cross_up"]
        ema9 = float(last.get("ema_9", 0))
        ema21 = float(last.get("ema_21", 0))
        ema55 = float(last.get("ema_55", 0))
        if ema21 <= 0 or ema55 <= 0:
            return None

        if entry > ema55:
            conditions.append("above_ema55")
        if ema9 > ema21:
            conditions.append("ema_aligned")
        if float(last.get("volume", 0)) > float(last.get("volume_ma", 1) or 1) * self.volume_factor:
            conditions.append("volume_spike")
        if last.get("macd_hist", 0) > prev.get("macd_hist", 0):
            conditions.append("macd_up")
        if self._body_ratio(last, bullish=True):
            conditions.append("bullish_body")

        return self._build_signal(symbol, "LONG", entry, conditions, float(last.get("wt1", 0)))

    def _check_short(self, symbol: str, df: pd.DataFrame) -> Optional[TradeSignal]:
        last = df.iloc[-1]
        prev = df.iloc[-2]
        entry = float(last["close"])

        if self.h1_enabled and not self._h1_allows(df, "SHORT"):
            return None
        if not bool(last.get("wt_cross_down", False)):
            return None

        conditions = ["markov_bear", "h1_trend", "wt_cross_down"] if self.h1_enabled else ["markov_bear", "wt_cross_down"]
        ema9 = float(last.get("ema_9", 0))
        ema21 = float(last.get("ema_21", 0))
        ema55 = float(last.get("ema_55", 0))
        if ema21 <= 0 or ema55 <= 0:
            return None

        if entry < ema55:
            conditions.append("below_ema55")
        if ema9 < ema21:
            conditions.append("ema_aligned_down")
        if float(last.get("volume", 0)) > float(last.get("volume_ma", 1) or 1) * self.volume_factor:
            conditions.append("volume_spike")
        if last.get("macd_hist", 0) < prev.get("macd_hist", 0):
            conditions.append("macd_down")
        if self._body_ratio(last, bullish=False):
            conditions.append("bearish_body")

        return self._build_signal(symbol, "SHORT", entry, conditions, float(last.get("wt1", 0)))

    def _build_signal(self, symbol: str, direction: str, entry: float, conditions: list, wt1: float) -> Optional[TradeSignal]:
        if len(conditions) < self.min_conditions:
            return None

        if direction == "LONG":
            target = entry * (1 + self.target_pct)
            stop = entry * (1 - self.stop_pct)
        else:
            target = entry * (1 - self.target_pct)
            stop = entry * (1 + self.stop_pct)

        rr = abs(target - entry) / abs(entry - stop)
        if rr < self.min_rr:
            return None

        sig = TradeSignal(
            symbol=symbol,
            direction=direction,
            entry_price=round(entry, 8),
            target_price=round(target, 8),
            stop_price=round(stop, 8),
            leverage=self.leverage,
            risk_reward=round(rr, 2),
            conditions_met=conditions,
            wt1_value=round(wt1, 2),
        )
        self._active[symbol] = sig
        logger.info(
            f"SINAL {direction} | {symbol} | entry={entry:.6f} "
            f"alvo={target:.6f}(+{self.target_pct*100:.1f}%) "
            f"stop={stop:.6f}(-{self.stop_pct*100:.1f}%) "
            f"R/R={rr:.2f} WT1={wt1:.1f} | {conditions}"
        )
        return sig

    def _h1_allows(self, df: pd.DataFrame, direction: str) -> bool:
        h1 = df.resample("1h").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna()
        if len(h1) < self.h1_ema_slow + 5:
            return False
        h1[f"ema_{self.h1_ema_fast}"] = h1["close"].ewm(span=self.h1_ema_fast, adjust=False).mean()
        h1[f"ema_{self.h1_ema_slow}"] = h1["close"].ewm(span=self.h1_ema_slow, adjust=False).mean()
        macd = h1["close"].ewm(span=12, adjust=False).mean() - h1["close"].ewm(span=26, adjust=False).mean()
        h1["macd_hist"] = macd - macd.ewm(span=9, adjust=False).mean()
        last = h1.iloc[-1]
        prev = h1.iloc[-2]

        if direction == "LONG":
            trend = last["close"] > last[f"ema_{self.h1_ema_slow}"] and last[f"ema_{self.h1_ema_fast}"] > last[f"ema_{self.h1_ema_slow}"]
            macd_ok = last["macd_hist"] > prev["macd_hist"] if self.h1_macd_confirm else True
            return bool(trend and macd_ok)

        trend = last["close"] < last[f"ema_{self.h1_ema_slow}"] and last[f"ema_{self.h1_ema_fast}"] < last[f"ema_{self.h1_ema_slow}"]
        macd_ok = last["macd_hist"] < prev["macd_hist"] if self.h1_macd_confirm else True
        return bool(trend and macd_ok)

    def _body_ratio(self, candle: pd.Series, bullish: bool) -> bool:
        candle_range = candle["high"] - candle["low"]
        if candle_range <= 0:
            return False
        body = (candle["close"] - candle["open"]) if bullish else (candle["open"] - candle["close"])
        return bool(body > 0 and body / candle_range >= self.body_ratio_min)

    def clear_signal(self, symbol: str) -> None:
        self._active.pop(symbol, None)
