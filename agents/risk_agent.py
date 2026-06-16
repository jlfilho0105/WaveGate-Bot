"""
AGENTE: RiskAgent
RESPONSABILIDADE: Gestão de risco — sizing de posição, limite de exposição e stop diário.
"""

import logging
from datetime import date
from .signal_agent import TradeSignal

logger = logging.getLogger(__name__)


class RiskAgent:
    def __init__(self, config: dict):
        self.risk_per_trade_pct   = config.get("risk_per_trade_pct",    1.0)
        self.max_open_positions   = config.get("max_open_positions",       3)
        self.daily_loss_limit_pct = config.get("daily_loss_limit_pct",   3.0)

        self._daily_loss = 0.0
        self._daily_date = date.today()

    def can_open(self, symbol: str, portfolio) -> bool:
        """True se RiskAgent permite abertura de nova posição."""
        self._reset_daily_if_needed()

        if len(portfolio.open_positions) >= self.max_open_positions:
            logger.debug(f"Máximo de posições abertas atingido ({self.max_open_positions})")
            return False

        if symbol in portfolio.open_positions:
            logger.debug(f"Já existe posição aberta em {symbol}")
            return False

        daily_limit = portfolio.equity * self.daily_loss_limit_pct / 100
        if self._daily_loss >= daily_limit:
            logger.warning(
                f"Stop diário atingido: {self._daily_loss:.2f} USDT "
                f"(limite {daily_limit:.2f} USDT)"
            )
            return False

        return True

    def size_position(self, signal: TradeSignal, equity: float) -> float:
        """
        Retorna margem em USDT a alocar.
        Fórmula: risco_usdt = equity × risk_pct / 100
                 notional   = risco_usdt / stop_pct
                 margin     = notional / leverage
        Teto: 30% do equity por posição.
        """
        risk_usdt = equity * self.risk_per_trade_pct / 100
        stop_frac = signal.stop_pct / 100
        if stop_frac <= 0:
            return 0.0
        notional  = risk_usdt / stop_frac
        margin    = notional / signal.leverage
        max_margin = equity * 0.30
        return round(min(margin, max_margin), 2)

    def register_result(self, pnl_usdt: float) -> None:
        """Registra resultado de um trade encerrado."""
        self._reset_daily_if_needed()
        if pnl_usdt < 0:
            self._daily_loss += abs(pnl_usdt)
            logger.info(f"Perda registrada: {pnl_usdt:.2f} USDT | acumulado hoje: {self._daily_loss:.2f}")

    @property
    def daily_loss(self) -> float:
        self._reset_daily_if_needed()
        return self._daily_loss

    def _reset_daily_if_needed(self) -> None:
        today = date.today()
        if self._daily_date != today:
            self._daily_loss = 0.0
            self._daily_date = today
