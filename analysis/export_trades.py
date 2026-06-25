"""
WaveGate Bot — Exportação de trades do backtest para CSV/Excel.
Gera: backtest/results/trades_ETHUSDT.csv  +  trades_summary_monthly.csv
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["OPENBLAS_NUM_THREADS"] = "1"

import pandas as pd
import numpy as np

import analysis.strategy_analysis as sa

EXPORT_SYMBOLS = ["ETHUSDT"]
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "backtest", "results")
os.makedirs(OUT_DIR, exist_ok=True)


def run_and_export():
    all_trades = []

    for symbol in EXPORT_SYMBOLS:
        print(f"Baixando dados {symbol}...")
        import requests
        from datetime import datetime, timedelta

        def fetch(sym, interval, days):
            tf = {"5m": 5, "1d": 1440}
            cpd = (24 * 60) // tf.get(interval, 1440)
            total = days * cpd
            all_data, end_time = [], None
            url = "https://fapi.binance.com/fapi/v1/klines"
            while len(all_data) < total:
                params = {"symbol": sym, "interval": interval, "limit": 1000}
                if end_time:
                    params["endTime"] = end_time
                import time; time.sleep(0.2)
                r = requests.get(url, params=params, timeout=15)
                data = r.json()
                if not data:
                    break
                all_data = data + all_data
                end_time = data[0][0] - 1
                if len(data) < 1000:
                    break
            df = pd.DataFrame(all_data, columns=[
                "open_time","open","high","low","close","volume",
                "close_time","quote_vol","trades","tbv","tqv","_"
            ])
            for c in ["open","high","low","close","volume"]:
                df[c] = df[c].astype(float)
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
            df.set_index("open_time", inplace=True)
            return df[["open","high","low","close","volume"]].tail(total)

        df_m5 = fetch(symbol, "5m", 180)
        df_d   = fetch(symbol, "1d", 400)
        print(f"  {symbol}: {len(df_m5)} candles M5")

        result = sa.run_backtest(symbol, df_m5, df_d["close"])
        trades = result["trades"]
        print(f"  {len(trades)} trades encontrados")

        for t in trades:
            t["symbol"] = symbol
        all_trades.extend(trades)

    if not all_trades:
        print("Nenhum trade encontrado.")
        return

    df = pd.DataFrame(all_trades)
    df["time"] = pd.to_datetime(df["time"])
    df["date"] = df["time"].dt.date
    df["hour"] = df["time"].dt.hour
    df["weekday"] = df["time"].dt.day_name()
    df["month"] = df["time"].dt.to_period("M").astype(str)

    # Equity curve acumulada (1% risco por trade, 3x alavancagem)
    equity = sa.CONFIG["initial_equity_usdt"]
    risk   = sa.CONFIG["risk_per_trade_pct"] / 100
    lev    = sa.CONFIG["leverage"]
    tgt    = sa.CONFIG["target_pct"]
    stp    = sa.CONFIG["stop_pct"]

    eq_vals = [equity]
    for _, row in df.iterrows():
        if row["outcome"] == "WIN":
            equity *= (1 + risk * tgt / stp)
        elif row["outcome"] == "LOSS":
            equity *= (1 - risk)
        # TIMEOUT: pnl_pct já calculado no backtest mas nao afeta sizing aqui
        eq_vals.append(equity)
    df["equity_after"] = eq_vals[1:]

    # ── CSV principal ──────────────────────────────────────────────
    csv_path = os.path.join(OUT_DIR, "trades_ETHUSDT.csv")
    df.to_csv(csv_path, index=False, float_format="%.6f")
    print(f"\nCSV exportado: {csv_path}")

    # ── Resumo mensal ──────────────────────────────────────────────
    monthly = df.groupby("month").agg(
        trades   = ("outcome", "count"),
        wins     = ("outcome", lambda x: (x == "WIN").sum()),
        losses   = ("outcome", lambda x: (x == "LOSS").sum()),
        timeouts = ("outcome", lambda x: (x == "TIMEOUT").sum()),
        win_rate = ("outcome", lambda x: round((x == "WIN").mean() * 100, 1)),
        avg_pnl  = ("pnl_pct", lambda x: round(x.mean(), 4)),
        total_pnl= ("pnl_pct", lambda x: round(x.sum(), 4)),
        avg_dur  = ("dur_min", lambda x: round(x.mean(), 1)),
    ).reset_index()

    monthly_path = os.path.join(OUT_DIR, "trades_summary_monthly.csv")
    monthly.to_csv(monthly_path, index=False)
    print(f"Resumo mensal: {monthly_path}")

    # ── Impressão no console ───────────────────────────────────────
    print("\n" + "="*72)
    print("  ETHUSDT — TRADES INDIVIDUAIS (primeiros 20)")
    print("="*72)
    cols = ["time","outcome","entry","exit","pnl_pct","dur_min","n_conds","wt1"]
    print(df[cols].head(20).to_string(index=False))

    print("\n" + "="*72)
    print("  RESUMO MENSAL")
    print("="*72)
    print(monthly.to_string(index=False))

    print("\n" + "="*72)
    print("  DISTRIBUICAO POR RESULTADO")
    print("="*72)
    for outcome, grp in df.groupby("outcome"):
        print(f"  {outcome:8s}: {len(grp):3d} trades | "
              f"pnl_pct medio={grp['pnl_pct'].mean():+.4f}% | "
              f"dur media={grp['dur_min'].mean():.1f} min")

    print("\n" + "="*72)
    print("  TOP 5 PIORES TRADES")
    print("="*72)
    print(df.nsmallest(5, "pnl_pct")[["time","outcome","entry","exit","pnl_pct","dur_min"]].to_string(index=False))

    print("\n" + "="*72)
    print("  TOP 5 MELHORES TRADES")
    print("="*72)
    print(df.nlargest(5, "pnl_pct")[["time","outcome","entry","exit","pnl_pct","dur_min"]].to_string(index=False))

    print(f"\n  Equity final simulada: ${df['equity_after'].iloc[-1]:,.2f}")
    print(f"  Retorno total:         {(df['equity_after'].iloc[-1]/sa.CONFIG['initial_equity_usdt']-1)*100:+.2f}%")
    print("="*72)


if __name__ == "__main__":
    run_and_export()
