"""
AGENTE: DataAgent
RESPONSABILIDADE: Coleta dados de mercado da Binance Spot/Margin via REST e WebSocket.
"""

import asyncio
import json
import logging
from typing import Callable, Dict, List
import aiohttp
import pandas as pd
import websockets

logger = logging.getLogger(__name__)

BINANCE_REST = "https://api.binance.com"
BINANCE_WS   = "wss://stream.binance.com:9443"


class DataAgent:
    def __init__(self, config: dict):
        self.api_key     = config.get("binance_api_key", "")
        self.api_secret  = config.get("binance_api_secret", "")
        self.symbols: List[str] = [s.upper() for s in config.get("symbols", [])]
        self.timeframe   = config.get("timeframe", "5m")
        self.buffer_size = config.get("buffer_size", 200)

        self._candle_buffer: Dict[str, pd.DataFrame] = {}
        self._callbacks: Dict[str, List[Callable]]   = {}
        self._ws_tasks:  Dict[str, asyncio.Task]     = {}
        self._running = False

    async def get_candles(self, symbol: str, limit: int = 200) -> pd.DataFrame:
        url     = f"{BINANCE_REST}/api/v3/klines"
        params  = {"symbol": symbol, "interval": self.timeframe, "limit": min(limit, 1000)}
        headers = {"X-MBX-APIKEY": self.api_key} if self.api_key else {}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json()

        df = self._parse_klines(data)
        self._candle_buffer[symbol] = df.tail(self.buffer_size)
        return df

    async def get_candles_history(self, symbol: str, days: int = 180) -> pd.DataFrame:
        url     = f"{BINANCE_REST}/api/v3/klines"
        headers = {"X-MBX-APIKEY": self.api_key} if self.api_key else {}
        total_candles = days * 288  # 288 candles M5 por dia
        all_data = []
        end_time = None

        async with aiohttp.ClientSession() as session:
            while len(all_data) < total_candles:
                params = {"symbol": symbol, "interval": self.timeframe, "limit": 1000}
                if end_time:
                    params["endTime"] = end_time
                async with session.get(url, params=params, headers=headers) as resp:
                    resp.raise_for_status()
                    batch = await resp.json()
                if not batch:
                    break
                all_data = batch + all_data
                end_time = batch[0][0] - 1
                await asyncio.sleep(0.2)

        df = self._parse_klines(all_data[-total_candles:])
        logger.info(f"{symbol}: {len(df)} candles M5 baixados ({days} dias)")
        return df

    async def get_candles_history_tf(self, symbol: str, timeframe: str, days: int = 365) -> pd.DataFrame:
        url     = f"{BINANCE_REST}/api/v3/klines"
        headers = {"X-MBX-APIKEY": self.api_key} if self.api_key else {}
        tf_minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
        candles_per_day = (24 * 60) // tf_minutes.get(timeframe, 1440)
        total_candles   = days * candles_per_day
        all_data = []
        end_time = None

        async with aiohttp.ClientSession() as session:
            while len(all_data) < total_candles:
                params = {"symbol": symbol, "interval": timeframe, "limit": 1000}
                if end_time:
                    params["endTime"] = end_time
                async with session.get(url, params=params, headers=headers) as resp:
                    resp.raise_for_status()
                    batch = await resp.json()
                if not batch:
                    break
                all_data = batch + all_data
                end_time = batch[0][0] - 1
                await asyncio.sleep(0.2)

        df = self._parse_klines(all_data[-total_candles:])
        logger.info(f"{symbol} {timeframe}: {len(df)} candles ({days} dias)")
        return df

    async def get_daily_close(self, symbol: str, years: int = 3) -> pd.Series:
        df = await self.get_candles_history_tf(symbol, "1d", years * 365)
        return df["close"]

    async def subscribe(self, symbol: str, callback: Callable) -> None:
        symbol = symbol.upper()
        if symbol not in self._callbacks:
            self._callbacks[symbol] = []
        self._callbacks[symbol].append(callback)

    async def start(self) -> None:
        self._running = True
        logger.info(f"DataAgent iniciando | {len(self.symbols)} pares | {self.timeframe}")

        for symbol in self.symbols:
            try:
                await self.get_candles(symbol, self.buffer_size)
                logger.info(f"Buffer carregado: {symbol}")
            except Exception as e:
                logger.warning(f"Erro ao carregar buffer {symbol}: {e}")

        for symbol in self.symbols:
            task = asyncio.create_task(self._ws_stream(symbol))
            self._ws_tasks[symbol] = task

        await asyncio.gather(*self._ws_tasks.values(), return_exceptions=True)

    async def stop(self) -> None:
        self._running = False
        for task in self._ws_tasks.values():
            task.cancel()
        self._ws_tasks.clear()
        logger.info("DataAgent encerrado")

    async def _ws_stream(self, symbol: str) -> None:
        stream = f"{symbol.lower()}@kline_{self.timeframe}"
        url    = f"{BINANCE_WS}/ws/{stream}"

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    logger.info(f"WebSocket conectado: {symbol}")
                    async for raw in ws:
                        await self._handle_ws_message(symbol, json.loads(raw))
            except Exception as e:
                if self._running:
                    logger.warning(f"WebSocket {symbol} desconectado: {e} — reconectando em 5s")
                    await asyncio.sleep(5)

    async def _handle_ws_message(self, symbol: str, msg: dict) -> None:
        kline = msg.get("k", {})
        if not kline.get("x", False):
            return  # só processa candle fechado

        new_row = pd.DataFrame([{
            "open_time": pd.to_datetime(kline["t"], unit="ms"),
            "open":   float(kline["o"]),
            "high":   float(kline["h"]),
            "low":    float(kline["l"]),
            "close":  float(kline["c"]),
            "volume": float(kline["v"]),
        }]).set_index("open_time")

        buf = self._candle_buffer.get(symbol, pd.DataFrame())
        buf = pd.concat([buf, new_row]).tail(self.buffer_size)
        self._candle_buffer[symbol] = buf

        for cb in self._callbacks.get(symbol, []):
            try:
                await cb(symbol, buf.copy())
            except Exception as e:
                logger.error(f"Erro no callback {symbol}: {e}")

    @staticmethod
    def _parse_klines(data: list) -> pd.DataFrame:
        df = pd.DataFrame(data, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
        ])
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        return df[["open", "high", "low", "close", "volume"]]
