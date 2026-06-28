"""
AGENTE: MarkovAgent
RESPONSABILIDADE: Gate macro de regime — classifica Bull/Sideways/Bear com Markov diário.

No WaveGate, o regime direciona o lado operacional:
Bull libera LONG, Bear libera SHORT e Sideways bloqueia entradas salvo configuracao explicita.
"""

import logging
import datetime
import numpy as np
import pandas as pd
from pathlib import Path

logger = logging.getLogger(__name__)


class MarkovAgent:
    def __init__(self, config: dict):
        self.window     = config.get("markov_window",    20)
        self.threshold  = config.get("markov_threshold", 0.05)
        self.min_train  = config.get("markov_min_train", 30)
        self.cache_dir  = Path("data/markov_cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._memory: dict = {}  # {symbol: (date, regime, signal_value)}

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def get_regime(self, symbol: str, close_daily: pd.Series) -> str:
        """Retorna 'Bull', 'Sideways' ou 'Bear' para o regime atual."""
        if len(close_daily) < self.min_train:
            return "Sideways"

        rolling_return = close_daily.pct_change(self.window)
        labels = pd.Series(1, index=close_daily.index)   # 1 = Sideways
        labels[rolling_return >  self.threshold] = 2      # 2 = Bull
        labels[rolling_return < -self.threshold] = 0      # 0 = Bear
        labels = labels.loc[rolling_return.notna()]

        if len(labels) < self.min_train:
            return "Sideways"

        counts = np.zeros((3, 3))
        arr = labels.to_numpy(dtype=int)
        for i in range(len(arr) - 1):
            counts[arr[i], arr[i + 1]] += 1.0

        row_sums = counts.sum(axis=1, keepdims=True)
        row_sums[row_sums == 0] = 1.0
        P = counts / row_sums

        current = int(arr[-1])
        signal  = float(P[current, 2] - P[current, 0])  # P(Bull) - P(Bear)

        if signal > 0.10:
            return "Bull"
        elif signal < -0.10:
            return "Bear"
        return "Sideways"

    def is_bull(self, symbol: str, close_daily: pd.Series) -> bool:
        """Compatibilidade com fluxos long-only: True somente se o regime atual e Bull."""
        today = datetime.datetime.now(datetime.UTC).date()
        if symbol in self._memory:
            cached_date, cached_regime, _ = self._memory[symbol]
            if cached_date == today:
                return cached_regime == "Bull"

        regime = self.get_regime(symbol, close_daily)
        self._memory[symbol] = (today, regime, 0.0)
        logger.debug(f"Regime {symbol}: {regime}")
        return regime == "Bull"

    def get_regime_info(self, symbol: str) -> dict:
        """Retorna info do regime em cache para exibição no Telegram."""
        if symbol not in self._memory:
            return {"regime": "Desconhecido", "date": None}
        d, regime, _ = self._memory[symbol]
        return {"regime": regime, "date": str(d)}

    def update_cache(self, symbol: str, close_daily: pd.Series) -> None:
        cache_file = self.cache_dir / f"{symbol}_daily.csv"
        df = pd.DataFrame({"close": close_daily})
        df.to_csv(cache_file)

    def load_cache(self, symbol: str) -> pd.Series:
        cache_file = self.cache_dir / f"{symbol}_daily.csv"
        if not cache_file.exists():
            return pd.Series(dtype=float)
        df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
        return df["close"]
