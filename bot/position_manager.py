"""
Portfolio and risk management.

Tracks:
  • Current balance (starting + realised P&L)
  • Open positions with unrealised P&L
  • Daily drawdown vs starting-of-day balance
  • Kill switch state

The kill switch fires when daily drawdown > daily_drawdown_limit (20% default)
and prevents any new orders from being placed.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from bot.config import RiskConfig
from bot.database import Database, Trade, TradeStatus

log = logging.getLogger(__name__)


@dataclass
class OpenPosition:
    trade_id: int
    market_ticker: str
    asset: str
    side: str
    size: int           # number of contracts
    entry_price: float
    current_price: float = 0.0
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def unrealised_pnl(self) -> float:
        return (self.current_price - self.entry_price) * self.size

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.size


class PortfolioManager:
    """
    Thread/task-safe portfolio state with kill-switch logic.
    All monetary values are in USD.
    """

    def __init__(self, risk: RiskConfig, db: Database) -> None:
        self._risk = risk
        self._db = db
        self._lock = asyncio.Lock()

        self._balance: float = risk.portfolio_balance
        self._starting_balance: float = risk.portfolio_balance
        self._day_open_balance: float = risk.portfolio_balance
        self._peak_balance: float = risk.portfolio_balance
        self._current_day: date = date.today()

        self._open_positions: dict[int, OpenPosition] = {}  # trade_id → position
        self._daily_trades: int = 0
        self._daily_wins: int = 0
        self._total_trades: int = 0
        self._total_wins: int = 0

        self._kill_switch_active: bool = False
        self._kill_switch_reason: str = ""

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def balance(self) -> float:
        return self._balance

    @property
    def open_positions(self) -> dict[int, OpenPosition]:
        return dict(self._open_positions)

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch_active

    @property
    def kill_switch_reason(self) -> str:
        return self._kill_switch_reason

    @property
    def unrealised_pnl(self) -> float:
        return sum(p.unrealised_pnl for p in self._open_positions.values())

    @property
    def total_equity(self) -> float:
        return self._balance + self.unrealised_pnl

    @property
    def daily_pnl(self) -> float:
        return self.total_equity - self._day_open_balance

    @property
    def daily_drawdown(self) -> float:
        """Fraction of day-open balance lost today (0–1, positive means loss)."""
        if self._day_open_balance <= 0:
            return 0.0
        return max(0.0, (self._day_open_balance - self.total_equity) / self._day_open_balance)

    @property
    def win_rate(self) -> float:
        if self._total_trades == 0:
            return 0.0
        return self._total_wins / self._total_trades

    # ------------------------------------------------------------------
    # Day roll
    # ------------------------------------------------------------------

    async def check_day_roll(self) -> None:
        """Reset daily counters if we've crossed midnight."""
        today = date.today()
        async with self._lock:
            if today != self._current_day:
                log.info(
                    "Day rolled: %s → %s | day P&L: %.2f",
                    self._current_day, today, self.daily_pnl,
                )
                await self._db.upsert_daily_stats(
                    starting_balance=self._day_open_balance,
                    ending_balance=self.total_equity,
                    gross_pnl=self.daily_pnl,
                    num_trades=self._daily_trades,
                    num_wins=self._daily_wins,
                    max_drawdown=self.daily_drawdown,
                    peak_balance=self._peak_balance,
                )
                self._current_day = today
                self._day_open_balance = self.total_equity
                self._daily_trades = 0
                self._daily_wins = 0
                # Reset kill switch at start of new day
                if self._kill_switch_active:
                    log.info("Kill switch reset for new trading day")
                    self._kill_switch_active = False
                    self._kill_switch_reason = ""

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    def _check_kill_switch(self) -> None:
        if self._kill_switch_active:
            return
        dd = self.daily_drawdown
        if dd >= self._risk.daily_drawdown_limit:
            reason = (
                f"Daily drawdown {dd:.1%} >= limit {self._risk.daily_drawdown_limit:.1%}"
            )
            self._kill_switch_active = True
            self._kill_switch_reason = reason
            log.critical("KILL SWITCH ACTIVATED: %s", reason)

    def can_trade(self) -> tuple[bool, str]:
        """Returns (allowed, reason_if_not)."""
        if self._kill_switch_active:
            return False, f"Kill switch active: {self._kill_switch_reason}"
        # Check whether opening any new position would breach the cap
        open_cost = sum(p.cost_basis for p in self._open_positions.values())
        if open_cost / max(1.0, self._balance) >= self._risk.max_position_pct * 5:
            return False, "Too many concurrent open positions"
        return True, ""

    def position_allowed(self, cost_usd: float) -> tuple[bool, str]:
        """Check if a specific new position fits within risk limits."""
        allowed, reason = self.can_trade()
        if not allowed:
            return False, reason
        max_usd = self._balance * self._risk.max_position_pct
        if cost_usd > max_usd:
            return False, f"Position ${cost_usd:.2f} > max allowed ${max_usd:.2f}"
        return True, ""

    # ------------------------------------------------------------------
    # Position lifecycle
    # ------------------------------------------------------------------

    async def open_position(self, trade: Trade, trade_id: int) -> OpenPosition:
        position = OpenPosition(
            trade_id=trade_id,
            market_ticker=trade.market_ticker,
            asset=trade.asset,
            side=trade.side.value,
            size=int(trade.size),
            entry_price=trade.entry_price,
        )
        cost = trade.entry_price * trade.size
        async with self._lock:
            self._balance -= cost
            self._open_positions[trade_id] = position
            self._daily_trades += 1
            self._total_trades += 1
            self._check_kill_switch()
        log.info("Position opened: trade_id=%d cost=%.2f balance=%.2f", trade_id, cost, self._balance)
        return position

    async def close_position(self, trade_id: int, exit_price: float) -> Optional[float]:
        """
        Close a position at exit_price.  Returns realised P&L or None if not found.
        """
        async with self._lock:
            pos = self._open_positions.pop(trade_id, None)
            if pos is None:
                log.warning("close_position called for unknown trade_id=%d", trade_id)
                return None
            # Realised P&L: (exit - entry) * size
            pnl = (exit_price - pos.entry_price) * pos.size
            proceeds = exit_price * pos.size
            self._balance += proceeds
            if pnl > 0:
                self._daily_wins += 1
                self._total_wins += 1
            # Update peak
            if self._balance > self._peak_balance:
                self._peak_balance = self._balance
            self._check_kill_switch()
        await self._db.close_trade(trade_id, exit_price, pnl)
        log.info(
            "Position closed: trade_id=%d pnl=%.4f exit=%.4f balance=%.2f",
            trade_id, pnl, exit_price, self._balance,
        )
        return pnl

    async def mark_positions(self, market_ticker: str, current_price: float) -> None:
        """Update unrealised P&L for all open positions in a given market."""
        async with self._lock:
            for pos in self._open_positions.values():
                if pos.market_ticker == market_ticker:
                    pos.current_price = current_price
                    upnl = pos.unrealised_pnl
                    await self._db.update_position_price(market_ticker, current_price, upnl)

    async def snapshot_daily_stats(self) -> None:
        """Persist current daily stats to DB (called periodically)."""
        await self._db.upsert_daily_stats(
            starting_balance=self._day_open_balance,
            ending_balance=self.total_equity,
            gross_pnl=self.daily_pnl,
            num_trades=self._daily_trades,
            num_wins=self._daily_wins,
            max_drawdown=self.daily_drawdown,
            peak_balance=self._peak_balance,
        )
