"""
AGENTE: ExecutionAgent
RESPONSABILIDADE: Envia ordens reais na Binance Futures (LONG-ONLY).
  - Entrada: MARKET
  - Stop Loss: STOP_MARKET
  - Take Profit: TAKE_PROFIT_MARKET
  - Fecha posição: MARKET reversa
"""

import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode

import aiohttp

from .signal_agent import TradeSignal

logger = logging.getLogger(__name__)

FAPI = "https://fapi.binance.com"


class ExecutionAgent:
    def __init__(self, config: dict):
        self.api_key    = config.get("binance_api_key", "")
        self.api_secret = config.get("binance_api_secret", "")
        self.leverage   = config.get("leverage", 3)
        self.paper      = config.get("paper_trade", True)

    # ── utilidades internas ─────────────────────────────────────────────────

    def _sign(self, params: dict) -> str:
        qs = urlencode(params)
        return hmac.new(
            self.api_secret.encode(), qs.encode(), hashlib.sha256
        ).hexdigest()

    def _headers(self) -> dict:
        return {"X-MBX-APIKEY": self.api_key}

    async def _post(self, session: aiohttp.ClientSession, path: str, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        async with session.post(
            f"{FAPI}{path}", params=params, headers=self._headers()
        ) as r:
            data = await r.json()
            if r.status != 200:
                logger.error(f"Binance API erro {r.status}: {data}")
            return data

    async def _delete(self, session: aiohttp.ClientSession, path: str, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        async with session.delete(
            f"{FAPI}{path}", params=params, headers=self._headers()
        ) as r:
            data = await r.json()
            return data

    # ── alavancagem ─────────────────────────────────────────────────────────

    async def set_leverage(self, symbol: str) -> None:
        async with aiohttp.ClientSession() as s:
            r = await self._post(s, "/fapi/v1/leverage", {
                "symbol": symbol, "leverage": self.leverage
            })
            logger.info(f"Alavancagem {symbol}: {r}")

    # ── quantidade mínima (step size) ───────────────────────────────────────

    async def get_quantity(self, symbol: str, usdt_notional: float) -> float:
        """Retorna quantidade de contratos respeitando stepSize da Binance."""
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{FAPI}/fapi/v1/exchangeInfo") as r:
                info = await r.json()

        sym_info = next((x for x in info["symbols"] if x["symbol"] == symbol), None)
        if not sym_info:
            raise ValueError(f"Símbolo {symbol} não encontrado na exchange")

        # Preço atual para calcular quantidade
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{FAPI}/fapi/v1/ticker/price", params={"symbol": symbol}) as r:
                price_data = await r.json()
        price = float(price_data["price"])

        raw_qty = usdt_notional / price

        # Respeitar stepSize
        lot_filter = next(
            (f for f in sym_info["filters"] if f["filterType"] == "LOT_SIZE"), None
        )
        if lot_filter:
            step = float(lot_filter["stepSize"])
            decimals = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
            raw_qty = round(raw_qty - (raw_qty % step), decimals)

        return raw_qty

    # ── abertura de posição ─────────────────────────────────────────────────

    async def open_long(self, signal: TradeSignal) -> dict:
        """
        Abre LONG com:
          1. MARKET entry
          2. STOP_MARKET  (stop loss)
          3. TAKE_PROFIT_MARKET (take profit)
        Retorna dict com ids das ordens ou resultado simulado (paper).
        """
        symbol = signal.symbol
        notional = signal.position_size_usdt * self.leverage

        if self.paper:
            logger.info(
                f"[PAPER] LONG {symbol} | notional=${notional:.2f} "
                f"entry={signal.entry_price:.4f} "
                f"tp={signal.target_price:.4f} sl={signal.stop_price:.4f}"
            )
            return {"paper": True, "symbol": symbol}

        await self.set_leverage(symbol)
        qty = await self.get_quantity(symbol, notional)
        if qty <= 0:
            logger.error(f"Quantidade inválida para {symbol}: {qty}")
            return {}

        async with aiohttp.ClientSession() as s:
            # 1. Entrada MARKET
            entry_r = await self._post(s, "/fapi/v1/order", {
                "symbol": symbol, "side": "BUY",
                "type": "MARKET", "quantity": qty,
            })
            logger.info(f"ENTRY {symbol}: {entry_r.get('orderId')} | qty={qty}")

            # 2. Stop Loss
            sl_r = await self._post(s, "/fapi/v1/order", {
                "symbol": symbol, "side": "SELL",
                "type": "STOP_MARKET",
                "stopPrice": round(signal.stop_price, 4),
                "quantity": qty,
                "reduceOnly": "true",
            })
            logger.info(f"SL {symbol}: {sl_r.get('orderId')} @ {signal.stop_price:.4f}")

            # 3. Take Profit
            tp_r = await self._post(s, "/fapi/v1/order", {
                "symbol": symbol, "side": "SELL",
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": round(signal.target_price, 4),
                "quantity": qty,
                "reduceOnly": "true",
            })
            logger.info(f"TP {symbol}: {tp_r.get('orderId')} @ {signal.target_price:.4f}")

        return {
            "entry_id": entry_r.get("orderId"),
            "sl_id":    sl_r.get("orderId"),
            "tp_id":    tp_r.get("orderId"),
            "qty":      qty,
        }

    # ── fechamento forçado (timeout) ────────────────────────────────────────

    async def close_long(self, symbol: str, qty: float) -> dict:
        """Fecha posição LONG com MARKET, cancela ordens pendentes (SL/TP)."""
        if self.paper:
            logger.info(f"[PAPER] CLOSE {symbol} qty={qty}")
            return {"paper": True}

        async with aiohttp.ClientSession() as s:
            # Cancela todas as ordens abertas do símbolo
            await self._delete(s, "/fapi/v1/allOpenOrders", {"symbol": symbol})

            # Fecha com MARKET
            r = await self._post(s, "/fapi/v1/order", {
                "symbol": symbol, "side": "SELL",
                "type": "MARKET", "quantity": qty,
                "reduceOnly": "true",
            })
            logger.info(f"CLOSE {symbol}: {r.get('orderId')}")
        return r

    # ── verificar posição aberta ────────────────────────────────────────────

    async def get_position(self, symbol: str) -> dict:
        """Retorna posição atual na Binance para o símbolo."""
        if self.paper:
            return {}
        async with aiohttp.ClientSession() as s:
            params = {"symbol": symbol}
            params["timestamp"] = int(time.time() * 1000)
            params["signature"] = self._sign(params)
            async with s.get(
                f"{FAPI}/fapi/v2/positionRisk",
                params=params, headers=self._headers()
            ) as r:
                data = await r.json()
        pos = next((p for p in data if p["symbol"] == symbol), {})
        return pos
