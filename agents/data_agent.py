"""
AGENTE: DataAgent
RESPONSABILIDADE: Coleta dados publicos da OKX para contratos USDT-SWAP.
"""

import asyncio
import json
import logging
from typing import Callable, Dict, List

import aiohttp
import pandas as pd
import websockets

logger = logging.getLogger(__name__)

OKX_REST = "https://www.okx.com"
OKX_WS = "wss://ws.okx.com:8443/ws/v5/public"


class DataAgent:
    def __init__(self, config: dict):
        self.symbols: List[str] = [s.upper() for s in config.get("symbols", [])]
        self.timeframe = config.get("timeframe", "5m")
        self.buffer_size = config.get("buffer_size", 1000)

        self._candle_buffer: Dict[str, pd.DataFrame] = {}
        self._callbacks: Dict[str, List[Callable]] = {}
        self._ws_tasks: Dict[str, asyncio.Task] = {}
        self._running = False

    async def get_candles(self, symbol: str, limit: int = 1000) -> pd.DataFrame:
        df = await self.get_candles_history(symbol, candles=limit)
        self._candle_buffer[symbol] = df.tail(self.buffer_size)
        return df

    async def get_candles_history(self, symbol: str, days: int | None = None, candles: int | None = None) -> pd.DataFrame:
        total = candles or (days or 180) * self._candles_per_day(self.timeframe)
        return await self._fetch_history(symbol, self._okx_bar(self.timeframe), total)

    async def get_candles_history_tf(self, symbol: str, timeframe: str, days: int = 365) -> pd.DataFrame:
        total = days * self._candles_per_day(timeframe)
        return await self._fetch_history(symbol, self._okx_bar(timeframe), total)

    async def get_daily_close(self, symbol: str, years: int = 3) -> pd.Series:
        df = await self._fetch_history(symbol, "1Dutc", years * 365)
        return df["close"]

    async def subscribe(self, symbol: str, callback: Callable) -> None:
        symbol = symbol.upper()
        self._callbacks.setdefault(symbol, []).append(callback)

    async def start(self) -> None:
        self._running = True
        logger.info(f"DataAgent OKX iniciando | {len(self.symbols)} instrumentos | {self.timeframe}")

        for symbol in self.symbols:
            try:
                await self.get_candles(symbol, self.buffer_size)
                logger.info(f"Buffer OKX carregado: {symbol}")
            except Exception as e:
                logger.warning(f"Erro ao carregar buffer {symbol}: {e}")

        for symbol in self.symbols:
            self._ws_tasks[symbol] = asyncio.create_task(self._ws_stream(symbol))

        await asyncio.gather(*self._ws_tasks.values(), return_exceptions=True)

    async def stop(self) -> None:
        self._running = False
        for task in self._ws_tasks.values():
            task.cancel()
        self._ws_tasks.clear()
        logger.info("DataAgent OKX encerrado")

    async def _fetch_history(self, symbol: str, bar: str, total: int) -> pd.DataFrame:
        rows = []
        after = None
        async with aiohttp.ClientSession() as session:
            while len(rows) < total:
                params = {"instId": symbol, "bar": bar, "limit": "300"}
                if after:
                    params["after"] = after
                async with session.get(f"{OKX_REST}/api/v5/market/history-candles", params=params) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                if data.get("code") != "0":
                    raise RuntimeError(f"OKX history-candles erro: {data}")
                batch = data.get("data", [])
                if not batch:
                    break
                rows.extend(batch)
                after = batch[-1][0]
                if len(batch) < 2:
                    break
                await asyncio.sleep(0.02)

        df = self._parse_okx_candles(rows)
        logger.info(f"{symbol} {bar}: {len(df)} candles")
        return df.tail(total)

    async def _ws_stream(self, symbol: str) -> None:
        channel = f"candle{self.timeframe}"
        sub = {"op": "subscribe", "args": [{"channel": channel, "instId": symbol}]}

        while self._running:
            try:
                async with websockets.connect(OKX_WS, ping_interval=20) as ws:
                    await ws.send(json.dumps(sub))
                    logger.info(f"WebSocket OKX conectado: {symbol} {channel}")
                    async for raw in ws:
                        msg = json.loads(raw)
                        if "event" in msg:
                            continue
                        await self._handle_ws_message(symbol, msg)
            except Exception as e:
                if self._running:
                    logger.warning(f"WebSocket OKX {symbol} desconectado: {e} - reconectando em 5s")
                    await asyncio.sleep(5)

    async def _handle_ws_message(self, symbol: str, msg: dict) -> None:
        rows = msg.get("data") or []
        if not rows:
            return
        row = rows[0]
        if len(row) < 9 or row[8] != "1":
            return

        new_row = self._parse_okx_candles([row])
        buf = self._candle_buffer.get(symbol, pd.DataFrame())
        buf = pd.concat([buf, new_row]).drop_duplicates().tail(self.buffer_size)
        self._candle_buffer[symbol] = buf

        for cb in self._callbacks.get(symbol, []):
            try:
                await cb(symbol, buf.copy())
            except Exception as e:
                logger.error(f"Erro no callback {symbol}: {e}")

    @staticmethod
    def _parse_okx_candles(data: list) -> pd.DataFrame:
        df = pd.DataFrame(
            data,
            columns=["ts", "open", "high", "low", "close", "volume", "vol_ccy", "vol_quote", "confirm"],
        )
        if df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = df[df["confirm"].astype(str) == "1"].copy()
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
        df.set_index("open_time", inplace=True)
        df.sort_index(inplace=True)
        return df[["open", "high", "low", "close", "volume"]]

    @staticmethod
    def _okx_bar(timeframe: str) -> str:
        if timeframe == "1d":
            return "1Dutc"
        return timeframe

    @staticmethod
    def _candles_per_day(timeframe: str) -> int:
        minutes = {
            "1m": 1,
            "3m": 3,
            "5m": 5,
            "15m": 15,
            "30m": 30,
            "1h": 60,
            "4h": 240,
            "1d": 1440,
        }.get(timeframe, 1440)
        return (24 * 60) // minutes
