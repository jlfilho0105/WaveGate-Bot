"""
AGENTE: IndicatorAgent
RESPONSABILIDADE: Calcula todos os indicadores técnicos sobre os candles M5.
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class IndicatorAgent:
    def __init__(self, config: dict):
        self.bb_period   = config.get("bb_period",      20)
        self.bb_std      = config.get("bb_std",          2)
        self.macd_fast   = config.get("macd_fast",      12)
        self.macd_slow   = config.get("macd_slow",      26)
        self.macd_signal = config.get("macd_signal",     9)
        self.rsi_period  = config.get("rsi_period",     14)
        self.volume_ma   = config.get("volume_ma_period", 20)
        self.ema_periods = config.get("ema_periods",  [9, 21, 55])
        self.ma_periods  = config.get("ma_periods",  [5, 10, 30, 60])

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df = self._add_ema(df)
        df = self._add_macd(df)
        df = self._add_rsi(df)
        df = self._add_volume_ma(df)
        df = self._add_bollinger(df)
        df = self._add_moving_averages(df)
        df = self._add_atr(df)
        return df

    def _add_ema(self, df: pd.DataFrame) -> pd.DataFrame:
        for p in self.ema_periods:
            df[f"ema_{p}"] = df["close"].ewm(span=p, adjust=False).mean()
        return df

    def _add_macd(self, df: pd.DataFrame) -> pd.DataFrame:
        ema_fast        = df["close"].ewm(span=self.macd_fast,   adjust=False).mean()
        ema_slow        = df["close"].ewm(span=self.macd_slow,   adjust=False).mean()
        df["macd_dif"]  = ema_fast - ema_slow
        df["macd_dea"]  = df["macd_dif"].ewm(span=self.macd_signal, adjust=False).mean()
        df["macd_hist"] = df["macd_dif"] - df["macd_dea"]
        return df

    def _add_rsi(self, df: pd.DataFrame) -> pd.DataFrame:
        delta       = df["close"].diff()
        gain        = delta.clip(lower=0).rolling(self.rsi_period).mean()
        loss        = (-delta.clip(upper=0)).rolling(self.rsi_period).mean()
        rs          = gain / loss.replace(0, np.nan)
        df["rsi"]   = 100 - (100 / (1 + rs))
        return df

    def _add_volume_ma(self, df: pd.DataFrame) -> pd.DataFrame:
        df["volume_ma"] = df["volume"].rolling(self.volume_ma).mean()
        return df

    def _add_bollinger(self, df: pd.DataFrame) -> pd.DataFrame:
        sma            = df["close"].rolling(self.bb_period).mean()
        std            = df["close"].rolling(self.bb_period).std()
        df["bb_mid"]   = sma
        df["bb_upper"] = sma + self.bb_std * std
        df["bb_lower"] = sma - self.bb_std * std
        df["bb_pct"]   = (df["close"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])
        return df

    def _add_moving_averages(self, df: pd.DataFrame) -> pd.DataFrame:
        for p in self.ma_periods:
            df[f"ma_{p}"] = df["close"].rolling(p).mean()
        return df

    def _add_atr(self, df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        high_low   = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift()).abs()
        low_close  = (df["low"]  - df["close"].shift()).abs()
        tr         = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df["atr"]  = tr.ewm(span=period, adjust=False).mean()
        return df
