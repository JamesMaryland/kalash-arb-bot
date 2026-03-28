"""
Trade executor.

Responsibilities:
  • Validate that the kill switch is off and risk gates pass
  • Translate an ArbOpportunity into a Trade record
  • Submit the order via KalshiClient (no-op in paper mode)
  • Open the position in PortfolioManager and persist to DB
  • Provide a close_position helper for expired/settled contracts
  • Deduplicate: never open two positions in the same market simultaneously
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from bot.arb_engine import ArbOpportunity
from bot.config import Config
from bot.database import Database, Side, Trade, TradeStatus
from bot.kalshi_client import KalshiClient
from bot.position_manager import PortfolioManager

log = logging.getLogger(__name__)

# Minimum seconds between trades on the same market (dedup window)
_DEDUP_WINDOW_SECS: float = 30.0


class TradeExecutor:
    def __init__(
        self,
        cfg: Config,
        kalshi: KalshiClient,
        portfolio: PortfolioManager,
        db: Database,
    ) -> None:
        self._cfg = cfg
        self._kalshi = kalshi
        self._portfolio = portfolio
        self._db = db
        self._is_paper = not cfg.live.is_live
        # market_ticker → last trade timestamp (dedup)
        self._last_trade_ts: dict[str, float] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def execute(self, opp: ArbOpportunity) -> Optional[int]:
        """
        Attempt to execute an arbitrage opportunity.
        Returns the trade_id on success, or None if skipped/rejected.
        """
        ticker = opp.quote.ticker

        async with self._lock:
            # Kill switch check
            can, reason = self._portfolio.can_trade()
            if not can:
                log.warning("Trade blocked [%s]: %s", ticker, reason)
                return None

            # Dedup: skip if we already have an open position in this market
            open_tickers = {p.market_ticker for p in self._portfolio.open_positions.values()}
            if ticker in open_tickers:
                log.debug("Dedup: already have open position in %s", ticker)
                return None

            # Recency dedup: don't trade the same market within dedup window
            last_ts = self._last_trade_ts.get(ticker, 0.0)
            if (time.monotonic() - last_ts) < _DEDUP_WINDOW_SECS:
                log.debug("Dedup window active for %s", ticker)
                return None

            # Position-size risk gate
            cost_usd = opp.entry_price * opp.max_contracts
            allowed, reason = self._portfolio.position_allowed(cost_usd)
            if not allowed:
                log.warning("Position rejected [%s]: %s", ticker, reason)
                return None

            # All gates passed — proceed
            self._last_trade_ts[ticker] = time.monotonic()

        # Build trade record
        contract_type = f"{opp.quote.duration}_{opp.quote.direction}"
        trade = Trade(
            market_ticker=ticker,
            asset=opp.quote.asset,
            contract_type=contract_type,
            side=Side(opp.side),
            size=float(opp.max_contracts),
            entry_price=opp.entry_price,
            fair_value=opp.fair_prob if opp.side == "YES" else 1.0 - opp.fair_prob,
            edge_pct=opp.edge,
            confidence=opp.confidence,
            kelly_fraction=self._cfg.risk.kelly_fraction,
            is_paper=self._is_paper,
            status=TradeStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
        )

        # Submit order
        order_id = await self._kalshi.place_order(
            ticker=ticker,
            side=opp.side,
            size=opp.max_contracts,
            price=opp.entry_price,
            is_paper=self._is_paper,
        )
        if order_id is None and not self._is_paper:
            log.error("Order placement failed for %s — skipping position open", ticker)
            return None

        trade.order_id = order_id

        # Persist and open position
        trade_id = await self._db.insert_trade(trade)
        await self._portfolio.open_position(trade, trade_id)

        log.info(
            "[%s] Trade opened: id=%d %s %s x%d @ %.4f | edge=%.1f%% conf=%.1f%%",
            "PAPER" if self._is_paper else "LIVE",
            trade_id,
            ticker,
            opp.side,
            opp.max_contracts,
            opp.entry_price,
            opp.edge * 100,
            opp.confidence * 100,
        )
        return trade_id

    async def close_expired(self, trade_id: int, settlement_price: float) -> Optional[float]:
        """
        Close a trade at its settlement price.
        settlement_price is 1.0 (won) or 0.0 (lost) for binary contracts.
        Returns realised P&L or None.
        """
        pnl = await self._portfolio.close_position(trade_id, settlement_price)
        if pnl is not None:
            log.info("Trade %d settled: price=%.2f pnl=%.4f", trade_id, settlement_price, pnl)
        return pnl

    async def close_at_market(
        self, trade_id: int, current_yes_price: float, side: str
    ) -> Optional[float]:
        """
        Manually close a position before expiry at current market price.
        Used for stop-loss or manual exit.
        """
        exit_price = current_yes_price if side == "YES" else (1.0 - current_yes_price)
        return await self._portfolio.close_position(trade_id, exit_price)
