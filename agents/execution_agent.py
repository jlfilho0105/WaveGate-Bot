"""
AGENTE: ExecutionAgent - OKX USDT-SWAP
RESPONSABILIDADE: Executa entradas LONG/SHORT e fechamentos reducao-only.
"""

import base64
import hashlib
import hmac
import json
import logging
import math
from datetime import datetime, timezone

import aiohttp

from .signal_agent import TradeSignal

logger = logging.getLogger(__name__)

OKX_REST = "https://www.okx.com"


class ExecutionAgent:
    def __init__(self, config: dict):
        self.api_key = config.get("okx_api_key", "")
        self.api_secret = config.get("okx_api_secret", "")
        self.passphrase = config.get("okx_api_passphrase", "")
        self.leverage = int(config.get("leverage", 3))
        self.td_mode = config.get("td_mode", "cross")
        self.simulated = bool(config.get("okx_simulated_trading", False))
        self.paper = config.get("paper_trade", True)
        self._instrument_cache: dict[str, dict] = {}

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        ts = self._timestamp()
        payload = f"{ts}{method.upper()}{path}{body}"
        sign = base64.b64encode(
            hmac.new(self.api_secret.encode(), payload.encode(), hashlib.sha256).digest()
        ).decode()
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        if self.simulated:
            headers["x-simulated-trading"] = "1"
        return headers

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload, separators=(",", ":")) if payload else ""
        headers = self._headers(method, path, body)
        async with aiohttp.ClientSession() as session:
            async with session.request(method, f"{OKX_REST}{path}", data=body or None, headers=headers) as resp:
                data = await resp.json()
        if data.get("code") != "0":
            logger.error(f"OKX erro {method} {path}: {data}")
        return data

    async def _public_get(self, path: str, params: dict) -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{OKX_REST}{path}", params=params) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def _get_price(self, symbol: str) -> float:
        data = await self._public_get("/api/v5/market/ticker", {"instId": symbol})
        return float(data["data"][0]["last"])

    async def _get_instrument(self, symbol: str) -> dict:
        if symbol in self._instrument_cache:
            return self._instrument_cache[symbol]
        data = await self._public_get("/api/v5/public/instruments", {"instType": "SWAP", "instId": symbol})
        inst = data["data"][0]
        self._instrument_cache[symbol] = inst
        return inst

    async def _contracts_for_notional(self, symbol: str, notional_usdt: float) -> float:
        price = await self._get_price(symbol)
        inst = await self._get_instrument(symbol)
        ct_val = float(inst.get("ctVal") or 1)
        lot = float(inst.get("lotSz") or 1)
        raw = notional_usdt / (price * ct_val)
        qty = math.floor(raw / lot) * lot
        decimals = max(0, int(round(-math.log10(lot)))) if lot < 1 else 0
        return round(qty, decimals)

    async def set_leverage(self, symbol: str) -> None:
        if self.paper:
            return
        await self._request("POST", "/api/v5/account/set-leverage", {
            "instId": symbol,
            "lever": str(self.leverage),
            "mgnMode": self.td_mode,
        })

    async def open_position(self, signal: TradeSignal) -> dict:
        symbol = signal.symbol
        notional = signal.position_size_usdt * signal.leverage

        if self.paper:
            logger.info(
                f"[PAPER] {signal.direction} {symbol} | notional=${notional:.2f} "
                f"entry~{signal.entry_price:.6f} tp={signal.target_price:.6f} sl={signal.stop_price:.6f}"
            )
            return {"paper": True, "symbol": symbol, "qty": 0.0}

        await self.set_leverage(symbol)
        qty = await self._contracts_for_notional(symbol, notional)
        if qty <= 0:
            logger.error(f"Quantidade OKX invalida para {symbol}: {qty}")
            return {}

        side = "buy" if signal.direction == "LONG" else "sell"
        payload = {
            "instId": symbol,
            "tdMode": self.td_mode,
            "side": side,
            "ordType": "market",
            "sz": str(qty),
            "attachAlgoOrds": [{
                "tpTriggerPx": str(signal.target_price),
                "tpOrdPx": "-1",
                "slTriggerPx": str(signal.stop_price),
                "slOrdPx": "-1",
            }],
        }
        result = await self._request("POST", "/api/v5/trade/order", payload)
        order_id = (result.get("data") or [{}])[0].get("ordId")
        logger.info(f"OKX ENTRY {signal.direction} {symbol}: ordId={order_id} qty={qty}")
        return {"entry_id": order_id, "qty": qty}

    async def close_position(self, signal: TradeSignal, qty: float) -> dict:
        if self.paper:
            logger.info(f"[PAPER] CLOSE {signal.direction} {signal.symbol} qty={qty}")
            return {"paper": True}

        if qty <= 0:
            logger.warning(f"Fechamento ignorado sem qty valida: {signal.symbol} qty={qty}")
            return {}

        side = "sell" if signal.direction == "LONG" else "buy"
        payload = {
            "instId": signal.symbol,
            "tdMode": self.td_mode,
            "side": side,
            "ordType": "market",
            "sz": str(qty),
            "reduceOnly": "true",
        }
        result = await self._request("POST", "/api/v5/trade/order", payload)
        order_id = (result.get("data") or [{}])[0].get("ordId")
        logger.info(f"OKX CLOSE {signal.direction} {signal.symbol}: ordId={order_id}")
        return result

    async def open_long(self, signal: TradeSignal) -> dict:
        return await self.open_position(signal)

    async def close_long(self, symbol: str, qty: float) -> dict:
        dummy = TradeSignal(
            symbol=symbol,
            direction="LONG",
            entry_price=0,
            target_price=0,
            stop_price=0,
            leverage=self.leverage,
            risk_reward=0,
            conditions_met=[],
        )
        return await self.close_position(dummy, qty)

    async def check_account(self) -> dict:
        if self.paper:
            return {"paper": True}
        return await self._request("GET", "/api/v5/account/balance")

    async def get_account_equity_usdt(self) -> float:
        if self.paper:
            return 0.0
        data = await self.check_account()
        if data.get("code") != "0":
            return 0.0
        rows = data.get("data") or []
        if not rows:
            return 0.0
        account = rows[0]
        for detail in account.get("details", []):
            if detail.get("ccy") == "USDT":
                for key in ("eq", "cashBal", "availEq"):
                    value = detail.get(key)
                    if value not in (None, ""):
                        return float(value)
        total = account.get("totalEq")
        return float(total) if total not in (None, "") else 0.0
