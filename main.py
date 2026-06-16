"""
WaveGate Bot — ponto de entrada.
Estratégia: WaveTrend M5 + Markov Gate Diário (LONG-ONLY, Binance Futures).
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


def load_config() -> dict:
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["binance_api_key"]    = os.getenv("BINANCE_API_KEY",    "")
    cfg["binance_api_secret"] = os.getenv("BINANCE_API_SECRET", "")
    cfg["telegram_token"]     = os.getenv("TELEGRAM_TOKEN",     "")
    cfg["telegram_chat_id"]   = os.getenv("TELEGRAM_CHAT_ID",  "")
    return cfg


async def main():
    config = load_config()

    # Inicializa todos os agentes
    data      = DataAgent(config)
    markov    = MarkovAgent(config)
    wave      = WaveAgent(config)
    indicator = IndicatorAgent(config)
    signal    = SignalAgent(config)
    risk      = RiskAgent(config)
    portfolio = PortfolioAgent(config)
    monitor   = MonitorAgent(config, portfolio=portfolio)
    telegram  = TelegramAgent(
        config,
        portfolio    = portfolio,
        on_start_cmd = data.start,
        on_stop_cmd  = data.stop,
    )

    # Conecta eventos do MonitorAgent → TelegramAgent
    monitor.on_event("on_target",   lambda s:          asyncio.create_task(telegram.send_target(s)))
    monitor.on_event("on_stop",     lambda s:          asyncio.create_task(telegram.send_stop(s)))
    monitor.on_event("on_timeout",  lambda s, p=0.0:   asyncio.create_task(telegram.send_timeout(s, p)))
    monitor.on_event("on_progress", lambda s, pct, px: asyncio.create_task(
        telegram.send_progress(s, pct, px)
    ))

    # Pré-carrega cache Markov diário (3 anos de dados)
    for sym in config["symbols"]:
        try:
            close_daily = await data.get_daily_close(sym, years=3)
            markov.update_cache(sym, close_daily)
            regime = markov.get_regime(sym, close_daily)
            logger.info(f"Cache Markov: {sym} → {regime}")
        except Exception as e:
            logger.warning(f"Erro ao carregar dados diários {sym}: {e}")

    # Callback chamado a cada candle M5 fechado
    async def on_new_candle(symbol: str, df):
        df_daily = markov.load_cache(symbol)
        if len(df_daily) < 30:
            return

        last_price = float(df.iloc[-1]["close"])

        # Gate Markov — só opera em regime Bull
        if not markov.is_bull(symbol, df_daily):
            monitor.update_price(symbol, last_price)
            return

        # Calcula indicadores e WaveTrend
        df_ind  = indicator.calculate(df)
        df_wave = wave.calculate(df_ind)

        # Avalia sinal
        trade_signal = signal.evaluate(symbol, df_wave, df_daily)

        if trade_signal:
            # Controle de risco antes de abrir
            if risk.can_open(symbol, portfolio):
                trade_signal.position_size_usdt = risk.size_position(
                    trade_signal, portfolio.equity
                )
                portfolio.open_position(trade_signal)
                await telegram.send_signal(trade_signal)
                monitor.start_monitoring(trade_signal)
        else:
            monitor.update_price(symbol, last_price)

    # Inscreve callback para cada par
    for sym in config["symbols"]:
        await data.subscribe(sym, on_new_candle)

    logger.info(
        f"WaveGate Bot iniciado — {len(config['symbols'])} pares | "
        f"Aguardando /start no Telegram."
    )
    await telegram.start()


if __name__ == "__main__":
    asyncio.run(main())
