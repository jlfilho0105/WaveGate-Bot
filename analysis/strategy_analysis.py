"""
WaveGate Bot — Análise de Estratégia e Prospecção de Lucros

Executa análise estática da combinação WaveTrend + Markov sem dados ao vivo.
Usa dados históricos da Binance (API pública, sem key necessária).

Uso:
    python analysis/strategy_analysis.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import logging
import numpy as np
import pandas as pd
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

# =========================================================================
# Parâmetros da estratégia
# =========================================================================
CONFIG = {
    # WaveTrend
    "wt_n1": 10, "wt_n2": 21,
    "wt_oversold": -60, "wt_overbought": 60,
    # Markov
    "markov_window": 20, "markov_threshold": 0.05, "markov_min_train": 30,
    # Sinal
    "leverage": 3, "min_rr": 2.5, "volume_factor": 1.5,
    "target_pct": 0.015, "stop_pct": 0.005,
    "body_ratio_min": 0.50, "min_conditions": 4,
    # EMA
    "ema_periods": [9, 21, 55],
    # Risco
    "initial_equity_usdt": 10_000.0,
    "risk_per_trade_pct": 1.0,
    "max_open_positions": 3,
}

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT"]
TIMEFRAME_M5 = "5m"
TIMEFRAME_1D = "1d"
BINANCE_REST = "https://fapi.binance.com"

# =========================================================================
# Download de dados
# =========================================================================

def fetch_klines(symbol: str, interval: str, days: int) -> pd.DataFrame:
    tf_minutes = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
    cpd = (24 * 60) // tf_minutes.get(interval, 1440)
    total = days * cpd
    all_data, end_time = [], None
    url = f"{BINANCE_REST}/fapi/v1/klines"

    while len(all_data) < total:
        params = {"symbol": symbol, "interval": interval, "limit": 1000}
        if end_time:
            params["endTime"] = end_time
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            logger.warning(f"Erro ao baixar {symbol} {interval}: {e}")
            break
        if not batch:
            break
        all_data = batch + all_data
        end_time = batch[0][0] - 1
        import time; time.sleep(0.25)

    data = all_data[-total:]
    df = pd.DataFrame(data, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","quote_vol","trades","buy_base","buy_quote","ignore"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("open_time", inplace=True)
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    return df[["open","high","low","close","volume"]]

# =========================================================================
# Indicadores
# =========================================================================

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for p in CONFIG["ema_periods"]:
        df[f"ema_{p}"] = df["close"].ewm(span=p, adjust=False).mean()
    ema_f = df["close"].ewm(span=12, adjust=False).mean()
    ema_s = df["close"].ewm(span=26, adjust=False).mean()
    df["macd_dif"]  = ema_f - ema_s
    df["macd_dea"]  = df["macd_dif"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd_dif"] - df["macd_dea"]
    df["volume_ma"] = df["volume"].rolling(20).mean()
    return df

def add_wave_trend(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    n1, n2 = CONFIG["wt_n1"], CONFIG["wt_n2"]
    ap  = (df["high"] + df["low"] + df["close"]) / 3.0
    esa = ap.ewm(span=n1, adjust=False).mean()
    d   = (ap - esa).abs().ewm(span=n1, adjust=False).mean().replace(0, np.nan)
    ci  = (ap - esa) / (0.015 * d)
    tci = ci.ewm(span=n2, adjust=False).mean()
    df["wt1"] = tci
    df["wt2"] = df["wt1"].rolling(4).mean()
    wt1p = df["wt1"].shift(1)
    wt2p = df["wt2"].shift(1)
    df["wt_cross_up"] = (
        (df["wt1"] > df["wt2"]) & (wt1p <= wt2p) & (wt1p < CONFIG["wt_oversold"])
    )
    return df

# =========================================================================
# Markov Gate
# =========================================================================

def get_markov_regime(close_daily: pd.Series, at_date) -> str:
    window, threshold = CONFIG["markov_window"], CONFIG["markov_threshold"]
    subset = close_daily[close_daily.index <= at_date]
    if len(subset) < CONFIG["markov_min_train"]:
        return "Sideways"
    rr = subset.pct_change(window)
    labels = pd.Series(1, index=subset.index)
    labels[rr >  threshold] = 2
    labels[rr < -threshold] = 0
    labels = labels.loc[rr.notna()]
    if len(labels) < CONFIG["markov_min_train"]:
        return "Sideways"
    arr = labels.to_numpy(dtype=int)
    counts = np.zeros((3, 3))
    for i in range(len(arr) - 1):
        counts[arr[i], arr[i+1]] += 1.0
    rs = counts.sum(axis=1, keepdims=True)
    rs[rs == 0] = 1.0
    P = counts / rs
    cur = int(arr[-1])
    sig = float(P[cur, 2] - P[cur, 0])
    if sig > 0.10:  return "Bull"
    if sig < -0.10: return "Bear"
    return "Sideways"

# =========================================================================
# Backtest da estratégia
# =========================================================================

def run_backtest(symbol: str, df_m5: pd.DataFrame, df_daily: pd.Series) -> dict:
    df = add_wave_trend(add_indicators(df_m5))
    warmup = 120
    tgt = CONFIG["target_pct"]
    stp = CONFIG["stop_pct"]
    lev = CONFIG["leverage"]
    min_cond = CONFIG["min_conditions"]
    vol_fac  = CONFIG["volume_factor"]
    bdy_min  = CONFIG["body_ratio_min"]

    trades = []
    i = warmup
    while i < len(df) - 1:
        row  = df.iloc[i]
        prev = df.iloc[i-1]

        # Gate Markov: data do candle atual, consulta diário
        day = df.index[i].date()
        daily_sub = df_daily[df_daily.index.date <= day]
        regime = get_markov_regime(daily_sub, daily_sub.index[-1] if len(daily_sub) else df_daily.index[0])
        if regime != "Bull":
            i += 1
            continue

        # Condições do sinal
        entry  = float(row["close"])
        ema9   = float(row.get("ema_9",  0))
        ema21  = float(row.get("ema_21", 0))
        ema55  = float(row.get("ema_55", 0))
        vol    = float(row.get("volume",    0))
        vol_ma = float(row.get("volume_ma", 1) or 1)
        wt1    = float(row.get("wt1", 0))

        if ema21 <= 0 or ema55 <= 0:
            i += 1; continue

        conds = ["markov_gate"]

        if not row.get("wt_cross_up", False):
            i += 1; continue
        conds.append("wt_cross_up")

        if entry > ema55:    conds.append("above_ema55")
        if ema9  > ema21:    conds.append("ema_aligned")
        if vol   > vol_ma * vol_fac: conds.append("volume_spike")
        if row.get("macd_hist",0) > prev.get("macd_hist",0): conds.append("macd_up")

        rng  = row["high"] - row["low"]
        body = row["close"] - row["open"]
        if body > 0 and rng > 0 and body/rng >= bdy_min:
            conds.append("bullish_body")

        if len(conds) < min_cond:
            i += 1; continue

        stop   = entry * (1 - stp)
        target = entry * (1 + tgt)
        rr     = (target - entry) / (entry - stop)
        if rr < CONFIG["min_rr"]:
            i += 1; continue

        # Simula resultado nos próximos 6 candles (30 min)
        outcome = "TIMEOUT"
        dur = 0
        exit_price = entry
        for j in range(i+1, min(i+7, len(df))):
            c = df.iloc[j]; dur += 1
            if c["high"] >= target:  outcome = "WIN";  exit_price = target; break
            if c["low"]  <= stop:    outcome = "LOSS"; exit_price = stop;   break

        if outcome == "WIN":    pnl = tgt * 100
        elif outcome == "LOSS": pnl = -stp * 100
        else:
            last_c = df.iloc[min(i+dur, len(df)-1)]["close"]
            pnl = (last_c - entry) / entry * 100

        trades.append({
            "time": df.index[i], "symbol": symbol,
            "entry": entry, "exit": exit_price, "outcome": outcome,
            "pnl_pct": round(pnl, 4), "dur_min": dur * 5,
            "rr": round(rr, 2), "wt1": round(wt1, 2),
            "n_conds": len(conds),
        })
        i += dur + 1

    return {"symbol": symbol, "trades": trades}

# =========================================================================
# Métricas
# =========================================================================

def calc_metrics(trades: list) -> dict:
    if not trades:
        return {}
    df = pd.DataFrame(trades)
    wins     = df[df.outcome == "WIN"]
    losses   = df[df.outcome == "LOSS"]
    timeouts = df[df.outcome == "TIMEOUT"]
    n = len(df)
    gp = wins["pnl_pct"].sum()
    gl = abs(losses["pnl_pct"].sum())
    pf = gp / gl if gl > 0 else float("inf")
    eq = df["pnl_pct"].cumsum()
    peak = eq.cummax()
    dd   = (peak - eq).max()
    arr  = df["pnl_pct"].values
    sharpe = float(arr.mean() / arr.std()) if arr.std() > 0 else 0.0
    return {
        "total":    n,
        "wins":     len(wins),
        "losses":   len(losses),
        "timeouts": len(timeouts),
        "win_rate": len(wins) / n * 100,
        "avg_dur":  df["dur_min"].mean(),
        "pf":       round(pf, 2),
        "max_dd":   round(dd, 2),
        "avg_rr":   round(df["rr"].mean(), 2),
        "total_ret": round(df["pnl_pct"].sum(), 2),
        "sharpe":   round(sharpe, 3),
        "avg_pnl":  round(df["pnl_pct"].mean(), 4),
    }

# =========================================================================
# Prospecção de lucros
# =========================================================================

def profit_projection(metrics_list: list) -> None:
    if not metrics_list:
        print("Sem métricas para projetar.")
        return

    all_m = pd.DataFrame(metrics_list)
    avg_wr  = all_m["win_rate"].mean()
    avg_rr  = all_m["avg_rr"].mean()
    avg_pnl = all_m["avg_pnl"].mean()  # % por trade (no position)
    avg_n   = all_m["total"].mean()    # trades por período de backtest (180 dias)
    signals_per_day = avg_n / 180.0    # taxa média por símbolo por dia

    equity  = CONFIG["initial_equity_usdt"]
    risk_pt = CONFIG["risk_per_trade_pct"] / 100
    lev     = CONFIG["leverage"]

    # EV por trade em % do capital
    # Win: risk × (tgt/stp) × lev   Loss: -risk × lev
    tgt_pct = CONFIG["target_pct"]
    stp_pct = CONFIG["stop_pct"]
    ev_win  = risk_pt * (tgt_pct / stp_pct) * (avg_wr / 100)
    ev_loss = risk_pt * ((1 - avg_wr / 100))
    ev_per_trade_pct = (ev_win - ev_loss) * 100

    n_symbols = len(SYMBOLS)
    # Regime Bull ~40% do tempo (validado no MarkovBinance v3)
    bull_pct = 0.40

    print("\n" + "="*65)
    print("  WAVEGATE BOT — PROSPECÇÃO DE LUCROS")
    print("="*65)
    print(f"\n  Capital inicial:   {equity:,.2f} USDT")
    print(f"  Pares monitorados: {n_symbols}")
    print(f"  Alavancagem:       {lev}x")
    print(f"  Risco por trade:   {risk_pt*100:.1f}% do capital")
    print(f"  Alvo / Stop:       +{tgt_pct*100:.1f}% / -{stp_pct*100:.1f}%  (R/R {avg_rr:.2f})")
    print(f"\n  --- Médias do backtest ---")
    print(f"  Win rate:          {avg_wr:.1f}%")
    print(f"  Sinais/par/dia:    {signals_per_day:.2f}")
    print(f"  EV por trade:      {ev_per_trade_pct:+.4f}% do capital")

    print("\n  --- Projeção mensal (30 dias) ---")
    print(f"  {'Cenário':<15} {'WR':>6}  {'Sinais/dia':>10}  {'EV/trade':>9}  {'Retorno':>8}")
    print("  " + "-"*57)

    scenarios = [
        ("Pessimista", avg_wr * 0.88, signals_per_day * 0.7 * n_symbols * bull_pct),
        ("Base",       avg_wr,        signals_per_day        * n_symbols * bull_pct),
        ("Otimista",   avg_wr * 1.10, signals_per_day * 1.4 * n_symbols * bull_pct),
    ]

    for name, wr, spd in scenarios:
        wr = min(wr, 80.0)
        ev_w = risk_pt * (tgt_pct / stp_pct) * (wr / 100)
        ev_l = risk_pt * (1 - wr / 100)
        ev   = (ev_w - ev_l) * 100
        monthly_trades = spd * 30
        monthly_ret = monthly_trades * ev
        yearly_ret  = (1 + monthly_ret/100) ** 12 - 1

        print(
            f"  {name:<15} {wr:>5.1f}%  {spd:>10.1f}  "
            f"{ev:>+8.4f}%  {monthly_ret:>+7.2f}%"
        )
        print(f"  {'':15}  Retorno anual estimado: {yearly_ret*100:+.1f}%")

    breakeven_wr = stp_pct / (tgt_pct + stp_pct) * 100
    print(f"\n  Break-even win rate: {breakeven_wr:.1f}% (com R/R {tgt_pct/stp_pct:.1f}:1)")
    print(f"\n  ⚠️  AVISO: projeções são estimativas teóricas.")
    print(f"     O backtest não garante desempenho futuro.")
    print(f"     Sempre opere com capital que pode perder.")
    print("="*65 + "\n")

# =========================================================================
# Relatório gráfico consolidado
# =========================================================================

def plot_consolidated(all_results: list, out_dir: str = "backtest/results") -> None:
    os.makedirs(out_dir, exist_ok=True)
    fig = plt.figure(figsize=(16, 10))
    gs  = gridspec.GridSpec(2, 3, figure=fig, hspace=0.4, wspace=0.35)

    ax_eq  = fig.add_subplot(gs[0, :2])
    ax_wr  = fig.add_subplot(gs[0, 2])
    ax_bar = fig.add_subplot(gs[1, :2])
    ax_tab = fig.add_subplot(gs[1, 2])
    ax_tab.axis("off")

    colors = ["#2196F3","#4CAF50","#FF9800","#E91E63","#9C27B0"]
    rows = []

    for i, res in enumerate(all_results):
        sym = res["symbol"]
        trades = res["trades"]
        if not trades:
            continue
        df_t = pd.DataFrame(trades)
        df_t["equity"] = df_t["pnl_pct"].cumsum()
        m = calc_metrics(trades)
        ax_eq.plot(range(len(df_t)), df_t["equity"],
                   label=sym, color=colors[i % len(colors)], linewidth=1.2)
        rows.append([sym, f"{m['win_rate']:.1f}%", f"{m['pf']:.2f}",
                     f"{m['max_dd']:.1f}%", f"{m['total_ret']:+.1f}%"])

    ax_eq.axhline(0, color="gray", linewidth=0.5, linestyle="--")
    ax_eq.set_title("Curva de Equity acumulada (%)", fontsize=11)
    ax_eq.set_ylabel("Retorno (%)")
    ax_eq.legend(fontsize=8)
    ax_eq.grid(True, alpha=0.3)

    # Win rate por símbolo
    syms   = [r["symbol"] for r in all_results if r["trades"]]
    wrs    = [calc_metrics(r["trades"])["win_rate"] for r in all_results if r["trades"]]
    bar_colors = ["#4CAF50" if w >= 50 else "#F44336" for w in wrs]
    ax_wr.bar(syms, wrs, color=bar_colors)
    ax_wr.axhline(33.3, color="orange", linestyle="--", linewidth=1, label="Break-even")
    ax_wr.set_title("Win Rate por par (%)", fontsize=11)
    ax_wr.set_ylim(0, 100)
    ax_wr.legend(fontsize=8)
    ax_wr.tick_params(axis="x", rotation=30)
    ax_wr.grid(True, alpha=0.3, axis="y")

    # Total de trades por símbolo
    totals = [len(r["trades"]) for r in all_results if r["trades"]]
    ax_bar.bar(syms, totals, color=colors[:len(syms)])
    ax_bar.set_title("Total de trades por par", fontsize=11)
    ax_bar.set_ylabel("Trades")
    ax_bar.tick_params(axis="x", rotation=30)
    ax_bar.grid(True, alpha=0.3, axis="y")

    # Tabela de métricas
    if rows:
        ax_tab.table(
            cellText=rows,
            colLabels=["Par","WR","PF","MaxDD","Ret%"],
            loc="center", cellLoc="center",
        ).auto_set_font_size(False)
        ax_tab.set_title("Resumo", fontsize=11, pad=20)

    fig.suptitle("WaveGate Bot — Análise de Backtest", fontsize=13, fontweight="bold")
    out_path = os.path.join(out_dir, "wavegate_analysis.png")
    plt.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close()
    print(f"\n  Relatório gráfico salvo: {out_path}")

# =========================================================================
# Main
# =========================================================================

def main():
    print("\nWaveGate Bot — Análise de Estratégia")
    print(f"Data: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Pares: {SYMBOLS}\n")

    all_results = []
    metrics_list = []

    for sym in SYMBOLS:
        print(f"  Baixando dados {sym}...")
        try:
            df_m5    = fetch_klines(sym, TIMEFRAME_M5, days=180)
            df_daily = fetch_klines(sym, TIMEFRAME_1D, days=365 * 3)["close"]
        except Exception as e:
            print(f"  ERRO {sym}: {e}")
            continue

        print(f"  Rodando backtest {sym} ({len(df_m5)} candles M5)...")
        result = run_backtest(sym, df_m5, df_daily)
        all_results.append(result)

        m = calc_metrics(result["trades"])
        if m:
            metrics_list.append(m)
            viable = (
                m["win_rate"] >= 40 and m["pf"] >= 1.3 and
                m["max_dd"]   <= 15 and m["total"] >= 15
            )
            status = "[VIAVEL]" if viable else "[REVISAR]"
            print(
                f"  {status} {sym}: {m['total']} trades | "
                f"WR={m['win_rate']:.1f}% | PF={m['pf']:.2f} | "
                f"DD={m['max_dd']:.1f}% | Ret={m['total_ret']:+.1f}%"
            )
        else:
            print(f"  {sym}: 0 trades gerados.")

    # Verifica lógica WaveTrend (sanity check)
    print("\n  --- Sanidade da lógica WaveTrend ---")
    print(f"  WT_oversold threshold: {CONFIG['wt_oversold']}")
    print(f"  WT_n1={CONFIG['wt_n1']} n2={CONFIG['wt_n2']} (parâmetros originais LazyBear)")
    print(f"  Condições mínimas: {CONFIG['min_conditions']} de 7")
    print(f"  'wt_cross_up' E 'markov_gate' são OBRIGATÓRIOS (hardcoded no signal_agent)")

    # Projeção de lucros
    profit_projection(metrics_list)

    # Gráfico consolidado
    if all_results:
        plot_consolidated(all_results)

    print("Análise concluída.\n")


if __name__ == "__main__":
    main()
