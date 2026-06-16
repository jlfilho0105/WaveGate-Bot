"""
AGENTE: BacktestAgent
RESPONSABILIDADE: Valida a estratégia WaveGate em dados históricos M5.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List
import pandas as pd
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    symbol: str
    total_trades:   int   = 0
    winning_trades: int   = 0
    losing_trades:  int   = 0
    timeout_trades: int   = 0
    win_rate:       float = 0.0
    avg_duration_min: float = 0.0
    profit_factor:  float = 0.0
    max_drawdown_pct: float = 0.0
    avg_rr:         float = 0.0
    total_return_pct: float = 0.0
    sharpe_ratio:   float = 0.0
    trades: List[dict] = field(default_factory=list)

    @property
    def is_viable(self) -> bool:
        breakeven_wr = 1 / (1 + self.avg_rr) * 100 if self.avg_rr > 0 else 60
        min_wr = min(breakeven_wr + 5, 45)
        return (
            self.win_rate       >= min_wr and
            self.profit_factor  >= 1.5 and
            self.max_drawdown_pct <= 12 and
            self.total_trades   >= 20 and
            self.avg_duration_min <= 35 and
            self.sharpe_ratio   >= 0.5
        )

    def summary(self) -> str:
        status = "[VIAVEL]" if self.is_viable else "[NAO VIAVEL]"
        return (
            f"{status} | {self.symbol}\n"
            f"  Trades: {self.total_trades} "
            f"(W:{self.winning_trades} L:{self.losing_trades} T:{self.timeout_trades})\n"
            f"  Win rate: {self.win_rate:.1f}% | Duração média: {self.avg_duration_min:.1f} min\n"
            f"  Profit factor: {self.profit_factor:.2f} | Max drawdown: {self.max_drawdown_pct:.1f}%\n"
            f"  R/R médio: {self.avg_rr:.2f} | Retorno total: {self.total_return_pct:.1f}%\n"
            f"  Sharpe: {self.sharpe_ratio:.2f}"
        )


class BacktestAgent:
    def __init__(self, config: dict, data_agent, indicator_agent, wave_agent,
                 signal_agent, markov_agent):
        self.config      = config
        self.data        = data_agent
        self.indicator   = indicator_agent
        self.wave        = wave_agent
        self.signal      = signal_agent
        self.markov      = markov_agent
        self.results_dir = config.get("results_dir", "backtest/results")
        self.history_days = config.get("history_days", 180)
        os.makedirs(self.results_dir, exist_ok=True)

    async def run_all(self, symbols: List[str]) -> Dict[str, BacktestResult]:
        results = {}
        for symbol in symbols:
            logger.info(f"Backtest iniciando: {symbol}")
            try:
                df       = await self.data.get_candles_history(symbol, self.history_days)
                df_daily = await self.data.get_daily_close(symbol, years=3)
                self.markov.update_cache(symbol, df_daily)
                result   = self.run(symbol, df, df_daily)
                results[symbol] = result
                logger.info(result.summary())
                self.save_report(symbol, result)
            except Exception as e:
                logger.error(f"Erro no backtest {symbol}: {e}", exc_info=True)
        return results

    def run(self, symbol: str, df: pd.DataFrame, df_daily: pd.Series) -> BacktestResult:
        df_ind  = self.indicator.calculate(df)
        df_wave = self.wave.calculate(df_ind)
        trades  = []
        warmup  = 120  # candles de aquecimento

        i = warmup
        while i < len(df_wave) - 1:
            window = df_wave.iloc[:i + 1]

            # Gate Markov sem look-ahead: só usa dados diários até a data do candle atual
            candle_date = df_wave.index[i].date()
            daily_avail = df_daily[df_daily.index.date <= candle_date]
            if len(daily_avail) < self.markov.min_train:
                i += 1
                continue
            if self.markov.get_regime(symbol, daily_avail) != "Bull":
                i += 1
                continue

            self.signal.clear_signal(symbol)
            sig = self.signal.evaluate(symbol, window, df_daily)

            if sig is None:
                i += 1
                continue

            outcome         = "TIMEOUT"
            duration_candles = 0
            exit_price      = sig.entry_price

            for j in range(i + 1, min(i + 7, len(df_wave))):  # max 6 candles = 30 min
                candle = df_wave.iloc[j]
                duration_candles += 1

                if candle["high"] >= sig.target_price:
                    outcome    = "WIN"
                    exit_price = sig.target_price
                    break
                if candle["low"] <= sig.stop_price:
                    outcome    = "LOSS"
                    exit_price = sig.stop_price
                    break

            close_idx = min(i + duration_candles, len(df_wave) - 1)
            close_exit = df_wave.iloc[close_idx]["close"]

            if outcome == "WIN":
                pnl = sig.target_pct
            elif outcome == "LOSS":
                pnl = -sig.stop_pct
            else:
                pnl = (close_exit - sig.entry_price) / sig.entry_price * 100

            trades.append({
                "symbol":       symbol,
                "direction":    sig.direction,
                "entry_time":   df_wave.index[i],
                "entry_price":  sig.entry_price,
                "target":       sig.target_price,
                "stop":         sig.stop_price,
                "exit_price":   exit_price,
                "outcome":      outcome,
                "duration_min": duration_candles * 5,
                "pnl_pct":      round(pnl, 4),
                "rr":           sig.risk_reward,
                "wt1":          sig.wt1_value,
                "conditions":   ",".join(sig.conditions_met),
            })

            i += duration_candles + 1

        return self._calc_metrics(symbol, trades)

    def save_report(self, symbol: str, result: BacktestResult) -> None:
        if not result.trades:
            return

        df_t = pd.DataFrame(result.trades)
        csv_path = os.path.join(self.results_dir, f"{symbol}_wavegate_trades.csv")
        df_t.to_csv(csv_path, index=False)

        df_t["equity"] = df_t["pnl_pct"].cumsum()

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8),
                                        gridspec_kw={"height_ratios": [3, 1]})
        fig.suptitle(
            f"WaveGate Bot — {symbol}\n{result.summary()}",
            fontsize=9, ha="left", x=0.02
        )

        ax1.plot(df_t.index, df_t["equity"], color="#2196F3", linewidth=1.5, label="Equity")
        ax1.axhline(0, color="gray", linewidth=0.5, linestyle="--")
        ax1.fill_between(df_t.index, df_t["equity"], 0,
                         where=df_t["equity"] >= 0, alpha=0.15, color="#4CAF50")
        ax1.fill_between(df_t.index, df_t["equity"], 0,
                         where=df_t["equity"] < 0, alpha=0.15, color="#F44336")
        ax1.set_ylabel("Retorno acumulado (%)")
        ax1.legend(loc="upper left")
        ax1.grid(True, alpha=0.3)

        colors = ["#4CAF50" if p > 0 else "#F44336" for p in df_t["pnl_pct"]]
        ax2.bar(df_t.index, df_t["pnl_pct"], color=colors, width=0.8)
        ax2.axhline(0, color="gray", linewidth=0.5)
        ax2.set_ylabel("PnL por trade (%)")
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()
        png_path = os.path.join(self.results_dir, f"{symbol}_wavegate_equity.png")
        plt.savefig(png_path, dpi=120, bbox_inches="tight")
        plt.close()
        logger.info(f"Relatório salvo: {csv_path} | {png_path}")

    def _calc_metrics(self, symbol: str, trades: list) -> BacktestResult:
        if not trades:
            return BacktestResult(symbol=symbol)

        wins     = [t for t in trades if t["outcome"] == "WIN"]
        losses   = [t for t in trades if t["outcome"] == "LOSS"]
        timeouts = [t for t in trades if t["outcome"] == "TIMEOUT"]

        gross_profit = sum(t["pnl_pct"] for t in wins)
        gross_loss   = abs(sum(t["pnl_pct"] for t in losses))
        if gross_loss == 0:
            gross_loss = gross_profit / 999 if gross_profit > 0 else 1.0

        # Equity curve e drawdown
        equity, peak, max_dd = 0.0, 0.0, 0.0
        returns = []
        for t in trades:
            equity += t["pnl_pct"]
            returns.append(t["pnl_pct"])
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd

        # Sharpe (trade-level, sem anualização aqui)
        import numpy as np
        arr = np.array(returns)
        sharpe = float(arr.mean() / arr.std()) if arr.std() > 0 else 0.0

        return BacktestResult(
            symbol           = symbol,
            total_trades     = len(trades),
            winning_trades   = len(wins),
            losing_trades    = len(losses),
            timeout_trades   = len(timeouts),
            win_rate         = len(wins) / len(trades) * 100,
            avg_duration_min = sum(t["duration_min"] for t in trades) / len(trades),
            profit_factor    = gross_profit / gross_loss,
            max_drawdown_pct = max_dd,
            avg_rr           = sum(t["rr"] for t in trades) / len(trades),
            total_return_pct = sum(t["pnl_pct"] for t in trades),
            sharpe_ratio     = round(sharpe, 3),
            trades           = trades,
        )
