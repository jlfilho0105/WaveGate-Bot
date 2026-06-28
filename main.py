"""
WaveGate Bot - OKX Directional.
Estrategia: Markov diario + H1 trend + WaveTrend M5 (LONG/SHORT).
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import yaml
from dotenv import load_dotenv

from agents import (
    DataAgent,
    ExecutionAgent,
    IndicatorAgent,
    MarkovAgent,
    MonitorAgent,
    PortfolioAgent,
    RiskAgent,
    SignalAgent,
    TelegramAgent,
    WaveAgent,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/wavegate.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

PAPER_TRADE = os.getenv("PAPER_TRADE", "true").lower() == "true"


def load_config() -> dict:
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["okx_api_key"] = os.getenv("OKX_API_KEY", "")
    cfg["okx_api_secret"] = os.getenv("OKX_API_SECRET", "")
    cfg["okx_api_passphrase"] = os.getenv("OKX_API_PASSPHRASE", "")
    cfg["telegram_token"] = os.getenv("TELEGRAM_TOKEN", "")
    cfg["telegram_chat_id"] = os.getenv("TELEGRAM_CHAT_ID", "")
    cfg["paper_trade"] = PAPER_TRADE
    return cfg


async def _refresh_markov_loop(data, markov, symbols: list[str]) -> None:
    """Atualiza o cache Markov diario uma vez por dia, 5 min apos meia-noite UTC."""
    while True:
        now = datetime.now(timezone.utc)
        next_run = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        wait_sec = (next_run - now).total_seconds()
        logger.info(f"Proximo refresh Markov em {wait_sec/3600:.1f}h ({next_run.strftime('%Y-%m-%d %H:%M UTC')})")
        await asyncio.sleep(wait_sec)
        for sym in symbols:
            try:
                close_daily = await data.get_daily_close(sym, years=3)
                markov.update_cache(sym, close_daily)
                regime = markov.get_regime(sym, close_daily)
                logger.info(f"[MARKOV REFRESH] {sym} -> {regime}")
            except Exception as e:
                logger.warning(f"Erro ao atualizar cache Markov {sym}: {e}")


async def reconcile_live_positions(execution, portfolio, symbols: list[str]) -> bool:
    portfolio.clear_open_positions()
    try:
        live_positions = await execution.get_open_positions(symbols)
    except Exception as e:
        logger.error(f"ERRO: nao foi possivel consultar posicoes abertas na OKX; abortando LIVE: {e}")
        return False
    if live_positions is None:
        logger.error("ERRO: consulta de posicoes abertas na OKX falhou; abortando LIVE")
        return False

    for symbol, pos in live_positions.items():
        if not all(key in pos for key in ("direction", "qty")):
            portfolio.clear_open_positions()
            logger.error(f"ERRO: posicao OKX incompleta para {symbol}; abortando LIVE")
            return False
        portfolio.protect_live_position(
            symbol,
            pos["direction"],
            pos["qty"],
            pos.get("avg_price", pos.get("entry_price", 0.0)),
            pos.get("target_price", pos.get("tp", 0.0)),
            pos.get("stop_price", pos.get("sl", 0.0)),
            pos.get("side"),
        )
    return True


async def main():
    config = load_config()
    use_telegram = bool(config["telegram_token"] and config["telegram_chat_id"])

    mode = "PAPER TRADE" if PAPER_TRADE else "LIVE"
    notif = "Telegram" if use_telegram else "Console/Log"
    logger.info(f"=== WaveGate OKX | Modo: {mode} | Notificacoes: {notif} ===")

    if not PAPER_TRADE and (
        not config["okx_api_key"] or not config["okx_api_secret"] or not config["okx_api_passphrase"]
    ):
        logger.error("ERRO: PAPER_TRADE=false mas credenciais OKX nao estao completas no .env")
        return

    data = DataAgent(config)
    markov = MarkovAgent(config)
    wave = WaveAgent(config)
    indicator = IndicatorAgent(config)
    signal = SignalAgent(config)
    risk = RiskAgent(config)
    portfolio = PortfolioAgent(config)
    monitor = MonitorAgent(config, portfolio=portfolio)
    execution = ExecutionAgent(config)

    if not PAPER_TRADE:
        okx_equity = await execution.get_account_equity_usdt()
        if okx_equity > 0:
            portfolio.sync_live_equity(okx_equity)
        else:
            logger.error("ERRO: nao foi possivel obter equity real da OKX; abortando LIVE")
            return

        if not await reconcile_live_positions(execution, portfolio, config["symbols"]):
            return

    telegram = TelegramAgent(config, portfolio=portfolio) if use_telegram else None

    def _on_target(s, pnl=0.0):
        risk.register_result(pnl)
        signal.clear_signal(s.symbol)
        logger.info(f"[WIN] {s.symbol} {s.direction} | equity=${portfolio.equity:.2f} | pnl={pnl:+.2f}")
        if telegram:
            asyncio.create_task(telegram.send_target(s))

    def _on_stop(s, pnl=0.0):
        risk.register_result(pnl)
        signal.clear_signal(s.symbol)
        logger.info(f"[LOSS] {s.symbol} {s.direction} | equity=${portfolio.equity:.2f} | pnl={pnl:+.2f}")
        if telegram:
            asyncio.create_task(telegram.send_stop(s))

    def _on_timeout(s, price=0.0, pnl=0.0, qty=0.0):
        risk.register_result(pnl)
        signal.clear_signal(s.symbol)
        logger.info(f"[TIMEOUT] {s.symbol} {s.direction} | equity=${portfolio.equity:.2f} | qty={qty}")
        if not PAPER_TRADE and qty > 0:
            asyncio.create_task(execution.close_position(s, qty))
        if telegram:
            asyncio.create_task(telegram.send_timeout(s, price))

    def _on_progress(s, pct, px):
        logger.info(f"[+{pct:.0f}%] {s.symbol} {s.direction} | price={px:.6f}")
        if telegram:
            asyncio.create_task(telegram.send_progress(s, pct, px))

    monitor.on_event("on_target", _on_target)
    monitor.on_event("on_stop", _on_stop)
    monitor.on_event("on_timeout", _on_timeout)
    monitor.on_event("on_progress", _on_progress)

    for sym in config["symbols"]:
        try:
            close_daily = await data.get_daily_close(sym, years=3)
            markov.update_cache(sym, close_daily)
            regime = markov.get_regime(sym, close_daily)
            logger.info(f"Cache Markov OKX: {sym} -> {regime}")
        except Exception as e:
            logger.warning(f"Erro ao carregar dados diarios {sym}: {e}")

    async def on_new_candle(symbol: str, df):
        df_daily = markov.load_cache(symbol)
        if len(df_daily) < 30:
            return

        last_price = float(df.iloc[-1]["close"])
        monitor.update_price(symbol, last_price)

        local_position = portfolio.open_positions.get(symbol)
        if local_position and local_position.get("live_protection") and not PAPER_TRADE:
            try:
                live_positions = await execution.get_open_positions([symbol])
            except Exception as e:
                logger.warning(f"Falha ao reconciliar posicao LIVE {symbol}: {e}")
                return
            if live_positions is None:
                logger.warning(f"Consulta OKX sem sucesso; mantendo protecao LIVE para {symbol}")
                return
            if symbol not in live_positions:
                portfolio.forget_open_position(symbol)
                signal.clear_signal(symbol)
            return

        if local_position:
            return

        regime = markov.get_regime(symbol, df_daily)
        if regime == "Sideways" and not config.get("trade_sideways", False):
            return

        df_ind = indicator.calculate(df)
        df_wave = wave.calculate(df_ind)
        trade_signal = signal.evaluate(symbol, df_wave, df_daily, regime=regime)

        if not trade_signal:
            return
        if not risk.can_open(symbol, portfolio):
            signal.clear_signal(symbol)
            return

        trade_signal.position_size_usdt = risk.size_position(trade_signal, portfolio.equity)
        try:
            result = await execution.open_position(trade_signal)
        except Exception as e:
            logger.error(f"Falha ao executar entrada {symbol}: {e}")
            signal.clear_signal(symbol)
            return
        if not result:
            signal.clear_signal(symbol)
            return

        portfolio.open_position(
            trade_signal,
            qty=float(result.get("qty", 0) or 0),
            entry_id=result.get("entry_id"),
        )
        monitor.start_monitoring(trade_signal)

        logger.info(
            f"[{'PAPER ' if PAPER_TRADE else ''}ENTRADA] {symbol} {trade_signal.direction} | "
            f"entry={trade_signal.entry_price:.6f} tp={trade_signal.target_price:.6f} "
            f"sl={trade_signal.stop_price:.6f} margin=${trade_signal.position_size_usdt:.2f}"
        )
        if telegram:
            await telegram.send_signal(trade_signal)

    for sym in config["symbols"]:
        await data.subscribe(sym, on_new_candle)

    logger.info(
        f"Bot OKX iniciado | {len(config['symbols'])} instrumento(s) | equity=${portfolio.equity:.2f}"
    )

    markov_refresh = _refresh_markov_loop(data, markov, config["symbols"])

    if telegram:
        async def start_telegram():
            okx_account = "DEMO" if config.get("okx_simulated_trading", False) else "REAL"
            asyncio.create_task(_send_startup_when_ready(telegram, mode, config["symbols"], portfolio.equity, okx_account))
            await telegram.start()

        await asyncio.gather(start_telegram(), data.start(), markov_refresh)
    else:
        await asyncio.gather(data.start(), markov_refresh)


async def _send_startup_when_ready(telegram: TelegramAgent, mode: str, symbols: list[str], equity: float, okx_account: str) -> None:
    for _ in range(20):
        if telegram._app:
            await telegram.send_startup(mode, symbols, equity, okx_account)
            return
        await asyncio.sleep(0.5)


if __name__ == "__main__":
    asyncio.run(main())
