"""
Telegram notification service.

Sends formatted messages for:
  • Every trade opened / closed (paper or live)
  • Kill switch activation
  • Drawdown threshold warnings (10%, 15%, 20%)
  • Daily summary

Uses python-telegram-bot in async mode.
Falls back to logging if the token/chat_id are missing or invalid.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from bot.arb_engine import ArbOpportunity
from bot.config import TelegramConfig

log = logging.getLogger(__name__)

# Drawdown alert thresholds (fraction)
_DRAWDOWN_THRESHOLDS = [0.10, 0.15, 0.20]


class TelegramNotifier:
    def __init__(self, cfg: TelegramConfig) -> None:
        self._cfg = cfg
        self._bot: Optional[object] = None
        self._enabled = False
        self._alerted_thresholds: set[float] = set()
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        try:
            from telegram import Bot
            self._bot = Bot(token=self._cfg.bot_token)
            # Quick connectivity test
            await self._bot.get_me()  # type: ignore[union-attr]
            self._enabled = True
            self._task = asyncio.create_task(self._sender_loop(), name="telegram-sender")
            log.info("Telegram notifier started (chat_id=%s)", self._cfg.chat_id)
        except Exception as exc:
            log.warning("Telegram unavailable — notifications disabled: %s", exc)
            self._enabled = False

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._bot:
            try:
                await self._bot.close()  # type: ignore[union-attr]
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public alert methods
    # ------------------------------------------------------------------

    async def trade_opened(self, trade_id: int, opp: ArbOpportunity, is_paper: bool) -> None:
        mode = "PAPER" if is_paper else "LIVE"
        signal_icons = {"COMBINED": "🔥", "ARB_ONLY": "📊", "TA_ONLY": "📈"}
        signal_icon = signal_icons.get(opp.signal_type.value, "📊")
        msg = (
            f"{'📝' if is_paper else '⚡'} *Trade Opened [{mode}]* `#{trade_id}`\n\n"
            f"*Signal:* {signal_icon} `{opp.signal_type.value}`\n"
            f"*Market:* `{opp.quote.ticker}`\n"
            f"*Asset:* {opp.quote.asset}  |  *Duration:* {opp.quote.duration}\n"
            f"*Side:* {opp.side}  |  *Size:* {opp.max_contracts} contracts\n"
            f"*Entry:* `${opp.entry_price:.4f}` per contract\n"
            f"*Fair Value:* `{opp.fair_prob:.3f}`  →  Kalshi: `{opp.kalshi_prob:.3f}`\n"
            f"*Edge:* `{opp.edge:.1%}`  |  *Confidence:* `{opp.confidence:.1%}`\n"
            f"*Kelly Size:* `${opp.kelly_size_usd:.2f}`\n"
            f"_⏰ {_now()}_"
        )
        await self._enqueue(msg)

    async def trade_closed(
        self,
        trade_id: int,
        ticker: str,
        pnl: float,
        exit_price: float,
        is_paper: bool,
    ) -> None:
        mode = "PAPER" if is_paper else "LIVE"
        icon = "✅" if pnl >= 0 else "❌"
        msg = (
            f"{icon} *Trade Closed [{mode}]* `#{trade_id}`\n\n"
            f"*Market:* `{ticker}`\n"
            f"*Exit Price:* `${exit_price:.4f}`\n"
            f"*P&L:* `{'+'if pnl>=0 else ''}{pnl:.4f}` USD\n"
            f"_⏰ {_now()}_"
        )
        await self._enqueue(msg)

    async def profit_lock_alert(self, balance: float, daily_pnl: float, target_pct: float) -> None:
        msg = (
            f"🔒 *Profit Lock Activated*\n\n"
            f"*Daily P&L:* `+${daily_pnl:.2f}` ({daily_pnl/max(1,balance):.1%})\n"
            f"*Target:* `{target_pct:.0%}` reached — no new trades until tomorrow\n"
            f"*Balance:* `${balance:.2f}`\n"
            f"_⏰ {_now()}_"
        )
        await self._enqueue(msg)

    async def kill_switch_alert(self, reason: str, balance: float, daily_dd: float) -> None:
        msg = (
            f"🚨 *KILL SWITCH ACTIVATED*\n\n"
            f"*Reason:* {reason}\n"
            f"*Balance:* `${balance:.2f}`\n"
            f"*Daily Drawdown:* `{daily_dd:.1%}`\n"
            f"⛔ *All trading halted for today.*\n"
            f"_⏰ {_now()}_"
        )
        await self._enqueue(msg)

    async def drawdown_warning(self, drawdown: float, balance: float) -> None:
        for threshold in _DRAWDOWN_THRESHOLDS:
            if drawdown >= threshold and threshold not in self._alerted_thresholds:
                self._alerted_thresholds.add(threshold)
                icon = "🔴" if threshold >= 0.20 else "🟠" if threshold >= 0.15 else "🟡"
                msg = (
                    f"{icon} *Drawdown Alert: {threshold:.0%}*\n\n"
                    f"*Current Drawdown:* `{drawdown:.2%}`\n"
                    f"*Balance:* `${balance:.2f}`\n"
                    f"_⏰ {_now()}_"
                )
                await self._enqueue(msg)

    async def daily_summary(
        self,
        balance: float,
        daily_pnl: float,
        num_trades: int,
        win_rate: float,
        daily_drawdown: float,
    ) -> None:
        pnl_icon = "📈" if daily_pnl >= 0 else "📉"
        msg = (
            f"{pnl_icon} *Daily Summary*\n\n"
            f"*Balance:* `${balance:.2f}`\n"
            f"*Day P&L:* `{'+'if daily_pnl>=0 else ''}{daily_pnl:.2f}` USD\n"
            f"*Trades:* `{num_trades}`  |  *Win Rate:* `{win_rate:.1%}`\n"
            f"*Max Drawdown:* `{daily_drawdown:.1%}`\n"
            f"_⏰ {_now()}_"
        )
        await self._enqueue(msg)
        # Reset drawdown alert flags for next day
        self._alerted_thresholds.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _enqueue(self, msg: str) -> None:
        if not self._enabled:
            log.info("TELEGRAM (disabled): %s", msg.replace("\n", " "))
            return
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            log.warning("Telegram send queue full — dropping message")

    async def _sender_loop(self) -> None:
        """
        Drains the queue, sending one message at a time with retry logic.
        Telegram rate limit is ~30 messages/second to same chat.
        """
        while True:
            try:
                msg = await self._queue.get()
                await self._send_with_retry(msg)
                self._queue.task_done()
                await asyncio.sleep(0.5)  # gentle rate limiting
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.error("Telegram sender loop error: %s", exc)
                await asyncio.sleep(5.0)

    async def _send_with_retry(self, msg: str, max_attempts: int = 3) -> None:
        backoff = 2.0
        for attempt in range(max_attempts):
            try:
                await self._bot.send_message(  # type: ignore[union-attr]
                    chat_id=self._cfg.chat_id,
                    text=msg,
                    parse_mode="Markdown",
                )
                return
            except Exception as exc:
                if attempt < max_attempts - 1:
                    log.warning("Telegram send failed (attempt %d): %s — retrying in %.0fs", attempt + 1, exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff *= 2
                else:
                    log.error("Telegram send permanently failed: %s", exc)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
