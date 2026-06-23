"""
AGENTE: ExecutionAgent — Binance Spot Margin (Cross Margin)
RESPONSABILIDADE: Ordens LONG via Margin para usuários sem acesso a Futuros.

Fluxo:
  1. Entrada: MARKET buy (com borrow automático pelo margin)
  2. SL + TP:  OCO sell (/sapi/v1/margin/order/oco)
  3. Timeout:  Cancela OCO + MARKET sell + repay
"""

import hashlib
import hmac
import logging
import math
import time
from urllib.parse import urlencode

import aiohttp

from .signal_agent import TradeSignal

logger = logging.getLogger(__name__)

SPOT = "https://api.binance.com"


class ExecutionAgent:
    def __init__(self, config: dict):
        self.api_key    = config.get("binance_api_key", "")
        self.api_secret = config.get("binance_api_secret", "")
        self.leverage   = config.get("leverage", 3)
        self.paper      = config.get("paper_trade", True)
        self._oco_ids: dict[str, int] = {}   # symbol → orderListId

    # ── Assinatura ──────────────────────────────────────────────────────────

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
            f"{SPOT}{path}", params=params, headers=self._headers()
        ) as r:
            data = await r.json()
            if r.status != 200:
                logger.error(f"Binance Margin erro {r.status} {path}: {data}")
            return data

    async def _delete(self, session: aiohttp.ClientSession, path: str, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        async with session.delete(
            f"{SPOT}{path}", params=params, headers=self._headers()
        ) as r:
            return await r.json()

    async def _get(self, session: aiohttp.ClientSession, path: str, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        async with session.get(
            f"{SPOT}{path}", params=params, headers=self._headers()
        ) as r:
            return await r.json()

    # ── Preço e step size ───────────────────────────────────────────────────

    async def _get_price(self, symbol: str) -> float:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{SPOT}/api/v3/ticker/price", params={"symbol": symbol}) as r:
                data = await r.json()
        return float(data["price"])

    async def _get_lot_info(self, symbol: str) -> tuple[float, int]:
        """Retorna (stepSize, price_decimals) do símbolo."""
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{SPOT}/api/v3/exchangeInfo", params={"symbol": symbol}) as r:
                info = await r.json()
        sym = info["symbols"][0]
        step = 1.0
        price_dec = 2
        for f in sym["filters"]:
            if f["filterType"] == "LOT_SIZE":
                step = float(f["stepSize"])
            if f["filterType"] == "PRICE_FILTER":
                tick = float(f["tickSize"])
                price_dec = max(0, int(round(-math.log10(tick))))
        qty_dec = max(0, int(round(-math.log10(step)))) if step < 1 else 0
        return step, qty_dec, price_dec

    def _floor_qty(self, qty: float, step: float, decimals: int) -> float:
        floored = math.floor(qty / step) * step
        return round(floored, decimals)

    # ── Verificar conta margin ───────────────────────────────────────────────

    async def check_account(self) -> dict:
        async with aiohttp.ClientSession() as s:
            data = await self._get(s, "/sapi/v1/margin/account", {})
        return data

    # ── Abertura de posição LONG ────────────────────────────────────────────

    async def open_long(self, signal: TradeSignal) -> dict:
        symbol   = signal.symbol
        notional = signal.position_size_usdt * self.leverage

        if self.paper:
            logger.info(
                f"[PAPER] LONG {symbol} | notional=${notional:.2f} "
                f"entry≈{signal.entry_price:.4f} "
                f"tp={signal.target_price:.4f} sl={signal.stop_price:.4f}"
            )
            return {"paper": True, "symbol": symbol, "qty": 0}

        price  = await self._get_price(symbol)
        step, qty_dec, price_dec = await self._get_lot_info(symbol)
        qty    = self._floor_qty(notional / price, step, qty_dec)
        tp     = round(signal.target_price, price_dec)
        sl     = round(signal.stop_price,   price_dec)
        sl_lim = round(sl * 0.999, price_dec)   # limit 0.1% abaixo do stop trigger

        if qty <= 0:
            logger.error(f"Quantidade invalida para {symbol}: {qty}")
            return {}

        async with aiohttp.ClientSession() as s:
            # 1. Compra MARKET com borrow automático (isAutoRepay=False → repay manual no close)
            entry_r = await self._post(s, "/sapi/v1/margin/order", {
                "symbol":      symbol,
                "side":        "BUY",
                "type":        "MARKET",
                "quantity":    qty,
                "sideEffectType": "MARGIN_BUY",   # borrow automático
            })
            logger.info(f"ENTRY MARGIN {symbol}: orderId={entry_r.get('orderId')} qty={qty}")

            # 2. OCO sell: TP + SL simultâneos
            oco_r = await self._post(s, "/sapi/v1/margin/order/oco", {
                "symbol":               symbol,
                "side":                 "SELL",
                "quantity":             qty,
                "price":                tp,          # LIMIT (take profit)
                "stopPrice":            sl,          # gatilho stop
                "stopLimitPrice":       sl_lim,      # preço limite do stop
                "stopLimitTimeInForce": "GTC",
                "sideEffectType":       "AUTO_REPAY",  # repay automático ao fechar
            })
            list_id = oco_r.get("orderListId", -1)
            self._oco_ids[symbol] = list_id
            logger.info(f"OCO {symbol}: listId={list_id} tp={tp} sl={sl}")

        return {
            "entry_id": entry_r.get("orderId"),
            "oco_list_id": list_id,
            "qty": qty,
        }

    # ── Fechamento forçado (timeout) ────────────────────────────────────────

    async def close_long(self, symbol: str, qty: float) -> dict:
        if self.paper:
            logger.info(f"[PAPER] CLOSE {symbol} qty={qty}")
            return {"paper": True}

        async with aiohttp.ClientSession() as s:
            # Cancela OCO pendente (se ainda ativo)
            list_id = self._oco_ids.pop(symbol, None)
            if list_id and list_id >= 0:
                await self._delete(s, "/sapi/v1/margin/orderList", {
                    "symbol": symbol, "orderListId": list_id
                })
                logger.info(f"OCO cancelado {symbol} listId={list_id}")

            # Vende MARKET com repay automático
            r = await self._post(s, "/sapi/v1/margin/order", {
                "symbol":         symbol,
                "side":           "SELL",
                "type":           "MARKET",
                "quantity":       qty,
                "sideEffectType": "AUTO_REPAY",
            })
            logger.info(f"CLOSE MARGIN {symbol}: orderId={r.get('orderId')}")
        return r

    # ── Posição aberta na conta margin ──────────────────────────────────────

    async def get_position(self, symbol: str) -> dict:
        if self.paper:
            return {}
        base_asset = symbol.replace("USDT", "")
        async with aiohttp.ClientSession() as s:
            data = await self._get(s, "/sapi/v1/margin/asset", {"asset": base_asset})
        return data
