"""
AGENTE: WaveAgent
RESPONSABILIDADE: Calcula o WaveTrend Oscillator e detecta crossovers em zonas extremas.

WaveTrend (LazyBear, 2014) — detecta sobrevenda/sobrecompra e reversões no M5.
Sinal de compra: WT1 cruza WT2 de baixo para cima saindo da zona de sobrevenda (<-60).
"""

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)


class WaveAgent:
    def __init__(self, config: dict):
        self.n1          = config.get("wt_n1", 10)
        self.n2          = config.get("wt_n2", 21)
        self.oversold    = config.get("wt_oversold",   -40)
        self.overbought  = config.get("wt_overbought",  60)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        """Adiciona WT1, WT2 e flags de crossover ao DataFrame."""
        df = df.copy()

        ap  = (df["high"] + df["low"] + df["close"]) / 3.0
        esa = ap.ewm(span=self.n1, adjust=False).mean()
        d   = (ap - esa).abs().ewm(span=self.n1, adjust=False).mean()

        # Evita divisão por zero
        d_safe = d.replace(0.0, np.nan)
        ci  = (ap - esa) / (0.015 * d_safe)
        tci = ci.ewm(span=self.n2, adjust=False).mean()

        df["wt1"] = tci
        df["wt2"] = df["wt1"].rolling(4).mean()

        wt1_prev = df["wt1"].shift(1)
        wt2_prev = df["wt2"].shift(1)

        # Crossover bullish: WT1 cruzou WT2 para cima
        wt_crosses_up = (df["wt1"] > df["wt2"]) & (wt1_prev <= wt2_prev)

        # Oversold recente: WT1 estava abaixo do threshold nas últimas 3 barras
        # (captura recuperações que iniciam antes do exato cruzamento)
        wt1_was_oversold = (
            (df["wt1"].shift(1) < self.oversold) |
            (df["wt1"].shift(2) < self.oversold) |
            (df["wt1"].shift(3) < self.oversold)
        )
        df["wt_cross_up"] = wt_crosses_up & wt1_was_oversold

        # Crossover bearish usado para sinais SHORT quando o regime Markov e Bear.
        wt_crosses_down = (df["wt1"] < df["wt2"]) & (wt1_prev >= wt2_prev)
        wt1_was_overbought = (
            (df["wt1"].shift(1) > self.overbought) |
            (df["wt1"].shift(2) > self.overbought) |
            (df["wt1"].shift(3) > self.overbought)
        )
        df["wt_cross_down"] = wt_crosses_down & wt1_was_overbought

        df["wt_oversold"]   = df["wt1"] < self.oversold
        df["wt_overbought"] = df["wt1"] > self.overbought
        df["wt_momentum"]   = df["wt1"] - df["wt2"]   # força da divergência

        return df

    def get_latest_status(self, df: pd.DataFrame) -> dict:
        """Retorna estado atual do WaveTrend para logging/Telegram."""
        last = df.iloc[-1]
        return {
            "wt1":         round(float(last.get("wt1", 0)), 2),
            "wt2":         round(float(last.get("wt2", 0)), 2),
            "cross_up":    bool(last.get("wt_cross_up",   False)),
            "cross_down":  bool(last.get("wt_cross_down", False)),
            "oversold":    bool(last.get("wt_oversold",   False)),
            "overbought":  bool(last.get("wt_overbought", False)),
        }
