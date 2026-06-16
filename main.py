"""
WaveGate Bot — ponto de entrada.
Estratégia: WaveTrend M5 + Markov Gate Diário (LONG-ONLY, Binance Futures).

Modos:
  PAPER_TRADE=true  → sem ordens reais, log de sinais no console/arquivo
  TELEGRAM_TOKEN    → se preenchido, envia notificações via Telegram
"""

import asyncio
import logging
import os
import yaml
from dotenv import load_dotenv

from agents import (
    DataAgent, WaveAgent, IndicatorAgent, SignalAgent,
    MarkovAgent, RiskAgent, PortfolioAgent, MonitorAgent, TelegramAgent
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


def load_config() -> dict:
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["binance_api_key"]    = os.getenv("BINANCE_API_KEY",    "")
    cfg["binance_api_secret"] = os.getenv("BINANCE_API_SECRET", "")
    cfg["telegram_token"]     = os.getenv("TELEGRAM_TOKEN",     "")
    cfg["telegram_chat_id"]   = os.getenv("TELEGRAM_CHAT_ID",  "")
    cfg["paper_trade"]        = PAPER_TRADE
    return cfg


def _log_signal(signal, tag: str = "SINAL") -> None:
    logger.info(
        f"[PAPER {tag}] {signal.symbol} | entrada={signal.entry_price:.4f} "
        f"alvo={signal.target_price:.4f} stop={signal.stop_price:.4f} "
        f"size=${signal.position_size_usdt:.2f} | conds={signal.conditions}"
    )


async def main():
    config = load_config()
    use_telegram = bool(config["telegram_token"] and config["telegram_chat_id"])

    mode = "PAPER TRADE" if PAPER_TRADE else "LIVE"
    notif = "Telegram" if use_telegram else "Console/Log"
    logger.info(f"=== WaveGate Bot | Modo: {mode} | Notificacoes: {notif} ===")

    # Inicializa agentes
    data      = DataAgent(config)
    markov    = MarkovAgent(config)
    wave      = WaveAgent(config)
    indicator = IndicatorAgent(config)
    signal    = SignalAgent(config)
    risk      = RiskAgent(config)
    portfolio = PortfolioAgent(config)
    monitor   = MonitorAgent(config, portfolio=portfolio)

    telegram = None
    if use_telegram:
        telegram = TelegramAgent(
            config,
            portfolio    = portfolio,
            on_start_cmd = data.start,
            on_stop_cmd  = data.stop,
        )
        monitor.on_event("on_target",   lambda s:          asyncio.create_task(telegram.send_target(s)))
        monitor.on_event("on_stop",     lambda s:          asyncio.create_task(telegram.send_stop(s)))
        monitor.on_event("on_timeout",  lambda s, p=0.0:   asyncio.create_task(telegram.send_timeout(s, p)))
        monitor.on_event("on_progress", lambda s, pct, px: asyncio.create_task(
            telegram.send_progress(s, pct, px)
        ))
    else:
        # Modo console: loga eventos de fechamento de posição
        def _on_target(s):
            pnl = portfolio.equity
            logger.info(f"[WIN]     {s.symbol} | equity=${pnl:.2f}")
        def _on_stop(s):
            pnl = portfolio.equity
            logger.info(f"[LOSS]    {s.symbol} | equity=${pnl:.2f}")
        def _on_timeout(s, p=0.0):
            pnl = portfolio.equity
            logger.info(f"[TIMEOUT] {s.symbol} | equity=${pnl:.2f}")
        def _on_progress(s, pct, px):
            logger.info(f"[+{pct:.0f}%] {s.symbol} | price={px:.4f}")

        monitor.on_event("on_target",   _on_target)
        monitor.on_event("on_stop",     _on_stop)
        monitor.on_event("on_timeout",  _on_timeout)
        monitor.on_event("on_progress", _on_progress)

    # Pré-carrega cache Markov diário
    for sym in config["symbols"]:
        try:
            close_daily = await data.get_daily_close(sym, years=3)
            markov.update_cache(sym, close_daily)
            regime = markov.get_regime(sym, close_daily)
            logger.info(f"Cache Markov: {sym} → {regime}")
        except Exception as e:
            logger.warning(f"Erro ao carregar dados diarios {sym}: {e}")

    # Callback chamado a cada candle M5 fechado
    async def on_new_candle(symbol: str, df):
        df_daily = markov.load_cache(symbol)
        if len(df_daily) < 30:
            return

        last_price = float(df.iloc[-1]["close"])

        if not markov.is_bull(symbol, df_daily):
            monitor.update_price(symbol, last_price)
            return

        df_ind  = indicator.calculate(df)
        df_wave = wave.calculate(df_ind)
        trade_signal = signal.evaluate(symbol, df_wave, df_daily)

        if trade_signal:
            if risk.can_open(symbol, portfolio):
                trade_signal.position_size_usdt = risk.size_position(
                    trade_signal, portfolio.equity
                )
                portfolio.open_position(trade_signal)
                _log_signal(trade_signal, "ENTRADA")
                if telegram:
                    await telegram.send_signal(trade_signal)
                monitor.start_monitoring(trade_signal)
        else:
            monitor.update_price(symbol, last_price)

    # Inscreve callback para cada par
    for sym in config["symbols"]:
        await data.subscribe(sym, on_new_candle)

    logger.info(
        f"Bot iniciado | {len(config['symbols'])} par(es) | equity=${portfolio.equity:.2f}"
        + (" | Aguardando /start no Telegram." if telegram else " | Iniciando stream automaticamente.")
    )

    if telegram:
        await telegram.start()
    else:
        # Sem Telegram: inicia stream direto
        await data.start()


if __name__ == "__main__":
    asyncio.run(main())
