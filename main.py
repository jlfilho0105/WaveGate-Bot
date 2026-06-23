"""
WaveGate Bot — ponto de entrada.
Estratégia: WaveTrend M5 + Markov Gate Diário (LONG-ONLY, Binance Futures).

Modos (definido em .env):
  PAPER_TRADE=true   → rastreia virtualmente, sem ordens reais
  PAPER_TRADE=false  → envia ordens reais na Binance Futures
"""

import asyncio
import logging
import os
import yaml
from dotenv import load_dotenv

from agents import (
    DataAgent, WaveAgent, IndicatorAgent, SignalAgent,
    MarkovAgent, RiskAgent, PortfolioAgent, MonitorAgent,
    TelegramAgent, ExecutionAgent,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/wavegate.log", encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)

PAPER_TRADE = os.getenv("PAPER_TRADE", "true").lower() == "true"

# Rastreia qty de cada posição aberta para fechar no timeout
_open_qty: dict[str, float] = {}


def load_config() -> dict:
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["binance_api_key"]    = os.getenv("BINANCE_API_KEY",    "")
    cfg["binance_api_secret"] = os.getenv("BINANCE_API_SECRET", "")
    cfg["telegram_token"]     = os.getenv("TELEGRAM_TOKEN",     "")
    cfg["telegram_chat_id"]   = os.getenv("TELEGRAM_CHAT_ID",  "")
    cfg["paper_trade"]        = PAPER_TRADE
    return cfg


async def main():
    config = load_config()
    use_telegram = bool(config["telegram_token"] and config["telegram_chat_id"])

    mode  = "PAPER TRADE" if PAPER_TRADE else "LIVE"
    notif = "Telegram" if use_telegram else "Console/Log"
    logger.info(f"=== WaveGate Bot | Modo: {mode} | Notificacoes: {notif} ===")

    if not PAPER_TRADE and (not config["binance_api_key"] or not config["binance_api_secret"]):
        logger.error("ERRO: PAPER_TRADE=false mas BINANCE_API_KEY/SECRET nao configurados no .env")
        return

    # Inicializa agentes
    data      = DataAgent(config)
    markov    = MarkovAgent(config)
    wave      = WaveAgent(config)
    indicator = IndicatorAgent(config)
    signal    = SignalAgent(config)
    risk      = RiskAgent(config)
    portfolio = PortfolioAgent(config)
    monitor   = MonitorAgent(config, portfolio=portfolio)
    execution = ExecutionAgent(config)

    # ── callbacks de fechamento de posição ──────────────────────────────────
    def _on_target(s):
        qty = _open_qty.pop(s.symbol, 0)
        logger.info(f"[WIN] {s.symbol} | equity=${portfolio.equity:.2f} | qty={qty}")
        if use_telegram:
            asyncio.create_task(telegram.send_target(s))

    def _on_stop(s):
        qty = _open_qty.pop(s.symbol, 0)
        logger.info(f"[LOSS] {s.symbol} | equity=${portfolio.equity:.2f} | qty={qty}")
        if use_telegram:
            asyncio.create_task(telegram.send_stop(s))

    def _on_timeout(s, p=0.0):
        qty = _open_qty.pop(s.symbol, 0)
        logger.info(f"[TIMEOUT] {s.symbol} | equity=${portfolio.equity:.2f}")
        if not PAPER_TRADE and qty > 0:
            asyncio.create_task(execution.close_long(s.symbol, qty))
        if use_telegram:
            asyncio.create_task(telegram.send_timeout(s, p))

    def _on_progress(s, pct, px):
        logger.info(f"[+{pct:.0f}%] {s.symbol} | price={px:.4f}")
        if use_telegram:
            asyncio.create_task(telegram.send_progress(s, pct, px))

    monitor.on_event("on_target",   _on_target)
    monitor.on_event("on_stop",     _on_stop)
    monitor.on_event("on_timeout",  _on_timeout)
    monitor.on_event("on_progress", _on_progress)

    # ── Telegram (opcional) ─────────────────────────────────────────────────
    telegram = None
    if use_telegram:
        telegram = TelegramAgent(
            config,
            portfolio = portfolio,
        )

    # ── Pré-carrega cache Markov ────────────────────────────────────────────
    for sym in config["symbols"]:
        try:
            close_daily = await data.get_daily_close(sym, years=3)
            markov.update_cache(sym, close_daily)
            regime = markov.get_regime(sym, close_daily)
            logger.info(f"Cache Markov: {sym} → {regime}")
        except Exception as e:
            logger.warning(f"Erro ao carregar dados diarios {sym}: {e}")

    # ── Callback por candle M5 ──────────────────────────────────────────────
    async def on_new_candle(symbol: str, df):
        df_daily   = markov.load_cache(symbol)
        if len(df_daily) < 30:
            return

        last_price = float(df.iloc[-1]["close"])

        if not markov.is_bull(symbol, df_daily):
            monitor.update_price(symbol, last_price)
            return

        df_ind       = indicator.calculate(df)
        df_wave      = wave.calculate(df_ind)
        trade_signal = signal.evaluate(symbol, df_wave, df_daily)

        if trade_signal and risk.can_open(symbol, portfolio):
            trade_signal.position_size_usdt = risk.size_position(
                trade_signal, portfolio.equity
            )
            # Executa ordem (real ou simulada)
            result = await execution.open_long(trade_signal)
            _open_qty[symbol] = result.get("qty", 0)

            portfolio.open_position(trade_signal)
            monitor.start_monitoring(trade_signal)

            logger.info(
                f"[{'PAPER ' if PAPER_TRADE else ''}ENTRADA] {symbol} | "
                f"entry={trade_signal.entry_price:.4f} "
                f"tp={trade_signal.target_price:.4f} "
                f"sl={trade_signal.stop_price:.4f} "
                f"size=${trade_signal.position_size_usdt:.2f}"
            )
            if telegram:
                await telegram.send_signal(trade_signal)
        else:
            monitor.update_price(symbol, last_price)

    # ── Inscreve pares ──────────────────────────────────────────────────────
    for sym in config["symbols"]:
        await data.subscribe(sym, on_new_candle)

    logger.info(
        f"Bot iniciado | {len(config['symbols'])} par(es) | equity=${portfolio.equity:.2f}"
        + (" | Aguardando /start no Telegram." if telegram else " | Iniciando stream automaticamente.")
    )

    if telegram:
        # Roda Telegram e stream de dados em paralelo
        await asyncio.gather(
            telegram.start(),
            data.start(),
        )
    else:
        await data.start()


if __name__ == "__main__":
    asyncio.run(main())
