"""
AGENTE: TelegramAgent
RESPONSABILIDADE: Interface Telegram — sinais, alertas e comandos de controle.
"""

import logging
from .signal_agent import TradeSignal, CONDITION_LABELS

logger = logging.getLogger(__name__)


class TelegramAgent:
    def __init__(self, config: dict, portfolio=None, on_start_cmd=None, on_stop_cmd=None):
        self.token        = config.get("telegram_token", "")
        self.chat_id      = config.get("telegram_chat_id", "")
        self._portfolio   = portfolio
        self.on_start_cmd = on_start_cmd
        self.on_stop_cmd  = on_stop_cmd
        self._app         = None

    async def start(self) -> None:
        from telegram.ext import Application, CommandHandler
        self._app = Application.builder().token(self.token).build()
        handlers = [
            ("start",    self._cmd_start),
            ("stop",     self._cmd_stop),
            ("status",   self._cmd_status),
            ("equity",   self._cmd_equity),
            ("regime",   self._cmd_regime),
            ("help",     self._cmd_help),
        ]
        for cmd, handler in handlers:
            self._app.add_handler(CommandHandler(cmd, handler))
        logger.info("TelegramAgent iniciado")
        await self._app.run_polling()

    # ------------------------------------------------------------------
    # Mensagens de sinal e monitoramento
    # ------------------------------------------------------------------

    async def send_signal(self, signal: TradeSignal) -> None:
        cond_str = "  ".join(
            CONDITION_LABELS.get(c, c) for c in signal.conditions_met
        )
        msg = (
            f"🌊 *WAVEGATE — LONG {signal.symbol}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Entrada:  `{signal.entry_price}`\n"
            f"Alvo:     `{signal.target_price}` \\(+{signal.target_pct:.2f}%\\)\n"
            f"Stop:     `{signal.stop_price}` \\(-{signal.stop_pct:.2f}%\\)\n"
            f"Margem:   `{signal.position_size_usdt:.2f} USDT`\n"
            f"Alavanca: {signal.leverage}x  R/R: {signal.risk_reward:.2f}  WT1: {signal.wt1_value}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{cond_str}"
        )
        await self._send(msg, parse_mode="MarkdownV2")

    async def send_target(self, signal: TradeSignal) -> None:
        gain_lev = signal.target_pct * signal.leverage
        pnl_usdt = signal.position_size_usdt * signal.leverage * (signal.target_pct / 100)
        msg = (
            f"✅ *ALVO ATINGIDO — {signal.symbol}*\n"
            f"Saída: `{signal.target_price}`\n"
            f"Ganho: +{signal.target_pct:.2f}% \\(+{gain_lev:.2f}% c/ {signal.leverage}x\\)\n"
            f"PnL: +{pnl_usdt:.2f} USDT"
        )
        await self._send(msg, parse_mode="MarkdownV2")

    async def send_stop(self, signal: TradeSignal) -> None:
        loss_lev = signal.stop_pct * signal.leverage
        pnl_usdt = signal.position_size_usdt * signal.leverage * (signal.stop_pct / 100)
        msg = (
            f"🔴 *STOP ATINGIDO — {signal.symbol}*\n"
            f"Saída: `{signal.stop_price}`\n"
            f"Perda: -{signal.stop_pct:.2f}% \\(-{loss_lev:.2f}% c/ {signal.leverage}x\\)\n"
            f"PnL: -{pnl_usdt:.2f} USDT"
        )
        await self._send(msg, parse_mode="MarkdownV2")

    async def send_progress(self, signal: TradeSignal, pct_done: float, current_price: float) -> None:
        msg = (
            f"📊 *{signal.symbol} — {pct_done:.0f}% do alvo*\n"
            f"Preço: `{current_price:.6f}` → Alvo: `{signal.target_price}`"
        )
        await self._send(msg, parse_mode="MarkdownV2")

    async def send_timeout(self, signal: TradeSignal, exit_price: float = 0) -> None:
        msg = (
            f"⚠️ *TIMEOUT — {signal.symbol}*\n"
            f"30 min sem atingir alvo — avalie fechamento manual\\.\n"
            f"Alvo: `{signal.target_price}` | Stop: `{signal.stop_price}`"
        )
        await self._send(msg, parse_mode="MarkdownV2")

    async def send_regime_update(self, symbol: str, regime: str) -> None:
        emoji = {"Bull": "🐂", "Bear": "🐻", "Sideways": "↔️"}.get(regime, "❓")
        msg = f"{emoji} *Regime {symbol}*: {regime}"
        await self._send(msg, parse_mode="MarkdownV2")

    # ------------------------------------------------------------------
    # Comandos
    # ------------------------------------------------------------------

    async def _cmd_start(self, update, context) -> None:
        if self.on_start_cmd:
            await self.on_start_cmd()
        await update.message.reply_text("▶️ WaveGate Bot iniciado.")

    async def _cmd_stop(self, update, context) -> None:
        if self.on_stop_cmd:
            await self.on_stop_cmd()
        await update.message.reply_text("⏹️ WaveGate Bot pausado.")

    async def _cmd_status(self, update, context) -> None:
        if self._portfolio:
            txt = f"📋 *Status WaveGate*\n{self._portfolio.summary()}"
        else:
            txt = "Status não disponível."
        await update.message.reply_text(txt, parse_mode="Markdown")

    async def _cmd_equity(self, update, context) -> None:
        if self._portfolio:
            p = self._portfolio
            txt = (
                f"💰 *Equity*: {p.equity:.2f} USDT\n"
                f"📈 PnL total: {p.total_pnl_usdt:+.2f} USDT ({p.total_pnl_pct:+.2f}%)\n"
                f"📊 Win rate: {p.win_rate:.1f}%\n"
                f"🔓 Trades fechados: {len(p.closed_trades)}"
            )
        else:
            txt = "Portfolio não disponível."
        await update.message.reply_text(txt, parse_mode="Markdown")

    async def _cmd_regime(self, update, context) -> None:
        await update.message.reply_text(
            "ℹ️ Regimes Markov atualizados diariamente ao carregar o bot.\n"
            "Gate ativo: somente operar em regime *Bull*.",
            parse_mode="Markdown"
        )

    async def _cmd_help(self, update, context) -> None:
        msg = (
            "🌊 *WaveGate Bot — Comandos*\n\n"
            "/start — inicia o scanner\n"
            "/stop — pausa o scanner\n"
            "/status — posições e métricas\n"
            "/equity — equity e P\\&L\n"
            "/regime — info do filtro Markov\n"
            "/help — este menu"
        )
        await update.message.reply_text(msg, parse_mode="MarkdownV2")

    # ------------------------------------------------------------------
    # Envio interno
    # ------------------------------------------------------------------

    async def _send(self, text: str, parse_mode: str = "Markdown") -> None:
        if not self._app:
            logger.warning("TelegramAgent não iniciado — mensagem descartada")
            return
        try:
            await self._app.bot.send_message(
                chat_id    = self.chat_id,
                text       = text,
                parse_mode = parse_mode,
            )
        except Exception as e:
            logger.error(f"Erro ao enviar Telegram: {e}")
