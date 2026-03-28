"""
Kalshi Latency Arbitrage Bot — main entry point.

Usage:
    python main.py [--paper]              # always safe — default mode
    python main.py [--enable-live-trading --confirm-live --override-live]

The three --*-live flags mirror LIVE_TRADING_ENABLE/CONFIRM/OVERRIDE in .env
and all must be present to override paper mode.  A single missing flag
leaves the bot in paper-trading mode.

Architecture (all tasks run concurrently via asyncio):
  ┌──────────────────────────────────────────────────────┐
  │  binance_feed         (WebSocket price stream)       │
  │  kalshi_poller        (periodic orderbook refresh)   │
  │  arb_scanner          (signal generation loop)       │
  │  settlement_checker   (marks expired contracts)      │
  │  daily_stats_flusher  (periodic DB snapshot + alert) │
  │  dashboard            (terminal UI render loop)      │
  │  kill_switch_monitor  (drawdown guard)               │
  └──────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone

from bot.arb_engine import ArbEngine
from bot.binance_feed import BinanceFeed
from bot.config import DB_PATH, Config
from bot.dashboard import Dashboard
from bot.database import Database
from bot.executor import TradeExecutor
from bot.kalshi_client import KalshiClient
from bot.position_manager import PortfolioManager
from bot.telegram_notifier import TelegramNotifier

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("arb_bot.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
# Suppress noisy third-party loggers
for _noisy in ("httpx", "httpcore", "websockets", "telegram"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Polling intervals
# ---------------------------------------------------------------------------
KALSHI_POLL_INTERVAL = 2.0       # seconds between orderbook refreshes
ARB_SCAN_INTERVAL = 0.5          # seconds between arb scan passes
SETTLEMENT_CHECK_INTERVAL = 30.0 # seconds between settlement checks
DAILY_STATS_INTERVAL = 300.0     # 5 minutes
MARKET_REFRESH_INTERVAL = 300.0  # re-discover Kalshi markets every 5 min

# Contract roll boundaries in minutes-past-the-hour.
# 15-min contracts roll at :00, :15, :30, :45.
# We trigger a forced refresh 5 seconds AFTER each boundary to give
# Kalshi time to open the new contract.
CONTRACT_ROLL_MINUTES = {0, 15, 30, 45}
CONTRACT_ROLL_OFFSET_SECS = 5   # seconds after the boundary to scan


# ---------------------------------------------------------------------------
# Main bot class
# ---------------------------------------------------------------------------

class ArbBot:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._db = Database(path=DB_PATH)
        self._binance = BinanceFeed()
        self._kalshi = KalshiClient(cfg.kalshi)
        self._portfolio = PortfolioManager(cfg.risk, self._db)
        self._engine = ArbEngine(cfg.risk)
        self._executor = TradeExecutor(cfg, self._kalshi, self._portfolio, self._db)
        self._telegram = TelegramNotifier(cfg.telegram)
        self._dashboard = Dashboard(
            portfolio=self._portfolio,
            binance=self._binance,
            kalshi=self._kalshi,
            is_paper=not cfg.live.is_live,
        )
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        log.info("=== Kalshi Arb Bot starting up ===")
        log.info("Mode: %s", "LIVE" if self._cfg.live.is_live else "PAPER")

        # Initialise persistent services
        await self._db.initialise()
        await self._telegram.start()
        await self._kalshi.start()

        # Start Binance feed (WebSocket)
        await self._binance.start()

        # Give Binance a moment to receive initial ticks
        await asyncio.sleep(2.0)

        # Recreate dashboard with updated references
        self._dashboard = Dashboard(
            portfolio=self._portfolio,
            binance=self._binance,
            kalshi=self._kalshi,
            is_paper=not self._cfg.live.is_live,
        )

        # Set up signal handlers for graceful shutdown
        # add_signal_handler is Unix-only; fall back to signal.signal on Windows
        loop = asyncio.get_running_loop()
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._request_shutdown)
        except NotImplementedError:
            # Windows — use threading signal handlers instead
            signal.signal(signal.SIGINT, lambda *_: self._request_shutdown())
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, lambda *_: self._request_shutdown())

        # Launch all concurrent tasks
        self._tasks = [
            asyncio.create_task(self._kalshi_poller(), name="kalshi-poller"),
            asyncio.create_task(self._arb_scanner(), name="arb-scanner"),
            asyncio.create_task(self._settlement_checker(), name="settlement-checker"),
            asyncio.create_task(self._daily_stats_flusher(), name="stats-flusher"),
            asyncio.create_task(self._kill_switch_monitor(), name="kill-switch-monitor"),
            asyncio.create_task(self._market_refresher(), name="market-refresher"),
            asyncio.create_task(self._contract_roll_watcher(), name="roll-watcher"),
        ]
        await self._dashboard.start()

        log.info("All tasks started.  Monitoring markets…")
        self._cfg.warn_paper_mode()

        # Wait until shutdown requested
        await self._shutdown_event.wait()
        await self._shutdown()

    def _request_shutdown(self) -> None:
        log.info("Shutdown signal received")
        self._shutdown_event.set()

    async def _shutdown(self) -> None:
        log.info("Shutting down gracefully…")

        # Cancel all tasks
        for task in self._tasks:
            task.cancel()
        results = await asyncio.gather(*self._tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                log.warning("Task error during shutdown: %s", r)

        await self._dashboard.stop()
        await self._binance.stop()
        await self._kalshi.stop()
        await self._portfolio.snapshot_daily_stats()
        await self._telegram.stop()
        log.info("Shutdown complete.")

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    async def _kalshi_poller(self) -> None:
        """Periodically refresh Kalshi orderbook quotes."""
        while not self._shutdown_event.is_set():
            try:
                await self._kalshi.refresh_quotes()
            except Exception as exc:
                log.warning("Kalshi poll error: %s", exc)
            await asyncio.sleep(KALSHI_POLL_INTERVAL)

    async def _arb_scanner(self) -> None:
        """
        Main arbitrage signal loop.
        For each live Kalshi quote, run it through the arb engine
        and execute if criteria are met.
        """
        while not self._shutdown_event.is_set():
            try:
                await self._scan_once()
            except Exception as exc:
                log.warning("Arb scan error: %s", exc)
            await asyncio.sleep(ARB_SCAN_INTERVAL)

    async def _scan_once(self) -> None:
        if self._portfolio.kill_switch_active:
            return

        portfolio_value = self._portfolio.total_equity

        for ticker, quote in list(self._kalshi.quotes.items()):
            asset_state = self._binance.get_state(quote.asset)
            if asset_state is None or asset_state.last_tick is None:
                continue  # no Binance data yet

            opp = self._engine.evaluate(quote, asset_state, portfolio_value)
            if opp is None:
                continue

            log.info("Opportunity found: %s", opp)

            trade_id = await self._executor.execute(opp)
            if trade_id is not None:
                await self._telegram.trade_opened(
                    trade_id, opp, is_paper=not self._cfg.live.is_live
                )
                # Refresh dashboard trades
                self._dashboard.set_recent_trades(await self._db.get_recent_trades(10))

    async def _settlement_checker(self) -> None:
        """
        Check open positions for expired contracts.
        For paper trading, we simulate settlement by checking if the
        Kalshi contract has a final YES price (1.0 or 0.0).
        For live trading, we check the actual settlement status.
        """
        while not self._shutdown_event.is_set():
            await asyncio.sleep(SETTLEMENT_CHECK_INTERVAL)
            try:
                await self._check_settlements()
            except Exception as exc:
                log.warning("Settlement check error: %s", exc)

    async def _check_settlements(self) -> None:
        open_trades = await self._db.get_open_trades()
        for t in open_trades:
            ticker = t["market_ticker"]
            trade_id = t["id"]

            # Check if market still has an active quote
            quote = self._kalshi.quotes.get(ticker)
            if quote is None:
                # Market no longer in quotes — attempt to look up settlement
                await self._settle_trade(trade_id, ticker, t)
                continue

            # Update unrealised P&L based on current mid price
            yes_mid = quote.yes_mid
            if t["side"] == "YES":
                current_price = yes_mid
            else:
                current_price = 1.0 - yes_mid
            await self._portfolio.mark_positions(ticker, current_price)

    async def _settle_trade(self, trade_id: int, ticker: str, trade_record: dict) -> None:
        """
        Attempt to determine settlement outcome for a trade whose market
        is no longer in the active quote feed.
        """
        # Try to fetch final market result from Kalshi
        try:
            resp = await self._kalshi._http.get(f"/markets/{ticker}")  # type: ignore[union-attr]
            if resp.status_code == 200:
                mkt = resp.json().get("market", {})
                result = mkt.get("result")  # "yes" or "no" after settlement
                if result:
                    won = (result.lower() == "yes" and trade_record["side"] == "YES") or \
                          (result.lower() == "no" and trade_record["side"] == "NO")
                    exit_price = 1.0 if won else 0.0
                    pnl = await self._executor.close_expired(trade_id, exit_price)
                    if pnl is not None:
                        await self._telegram.trade_closed(
                            trade_id, ticker, pnl, exit_price,
                            is_paper=not self._cfg.live.is_live,
                        )
                        self._dashboard.set_recent_trades(await self._db.get_recent_trades(10))
        except Exception as exc:
            log.debug("Settlement lookup failed for %s: %s", ticker, exc)

    async def _daily_stats_flusher(self) -> None:
        """Periodically persist daily stats and send Telegram summary."""
        last_summary_day = datetime.now(timezone.utc).date()
        while not self._shutdown_event.is_set():
            await asyncio.sleep(DAILY_STATS_INTERVAL)
            try:
                await self._portfolio.check_day_roll()
                await self._portfolio.snapshot_daily_stats()
                self._dashboard.set_recent_trades(await self._db.get_recent_trades(10))

                today = datetime.now(timezone.utc).date()
                if today != last_summary_day:
                    last_summary_day = today
                    await self._telegram.daily_summary(
                        balance=self._portfolio.balance,
                        daily_pnl=self._portfolio.daily_pnl,
                        num_trades=self._portfolio._daily_trades,
                        win_rate=self._portfolio.win_rate,
                        daily_drawdown=self._portfolio.daily_drawdown,
                    )
            except Exception as exc:
                log.warning("Stats flush error: %s", exc)

    async def _kill_switch_monitor(self) -> None:
        """
        Continuously monitor drawdown and fire Telegram alerts at thresholds.
        Also triggers the kill switch if the PortfolioManager hasn't already.
        """
        while not self._shutdown_event.is_set():
            await asyncio.sleep(5.0)
            try:
                dd = self._portfolio.daily_drawdown
                bal = self._portfolio.balance
                await self._telegram.drawdown_warning(dd, bal)

                if self._portfolio.kill_switch_active and not getattr(self, "_ks_alerted", False):
                    self._ks_alerted = True  # type: ignore[attr-defined]
                    await self._telegram.kill_switch_alert(
                        reason=self._portfolio.kill_switch_reason,
                        balance=bal,
                        daily_dd=dd,
                    )
                    log.critical(
                        "Kill switch: trading halted. Balance=%.2f DD=%.1%%", bal, dd
                    )
                elif not self._portfolio.kill_switch_active:
                    self._ks_alerted = False  # type: ignore[attr-defined]
            except Exception as exc:
                log.warning("Kill switch monitor error: %s", exc)

    async def _market_refresher(self) -> None:
        """Periodically re-discover Kalshi markets (contracts roll frequently)."""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(MARKET_REFRESH_INTERVAL)
            try:
                await self._kalshi._discover_markets()
                log.info("Market refresh: %d active markets", len(self._kalshi._active_tickers))
            except Exception as exc:
                log.warning("Market refresh error: %s", exc)

    async def _contract_roll_watcher(self) -> None:
        """
        Watches for 15-minute contract roll boundaries (:00, :15, :30, :45).
        Fires CONTRACT_ROLL_OFFSET_SECS after each boundary to:
          1. Re-discover markets (new contract just opened)
          2. Immediately refresh all orderbook quotes
          3. Run one aggressive arb scan pass
        This ensures we catch the freshest pricing on newly opened contracts
        before market makers have fully updated their quotes.
        """
        last_triggered_minute: int = -1

        while not self._shutdown_event.is_set():
            await asyncio.sleep(1.0)
            now = datetime.now(timezone.utc)
            minute = now.minute
            second = now.second

            # Fire once per boundary, CONTRACT_ROLL_OFFSET_SECS seconds after
            is_roll_boundary = minute in CONTRACT_ROLL_MINUTES
            is_trigger_window = CONTRACT_ROLL_OFFSET_SECS <= second < CONTRACT_ROLL_OFFSET_SECS + 10
            already_triggered = last_triggered_minute == minute

            if is_roll_boundary and is_trigger_window and not already_triggered:
                last_triggered_minute = minute
                log.info(
                    "Contract roll detected at %02d:%02d UTC — forcing market refresh + scan",
                    now.hour, minute,
                )
                try:
                    # Step 1: rediscover (new contracts may have opened)
                    await self._kalshi._discover_markets()
                    # Step 2: immediately poll all quotes
                    await self._kalshi.refresh_quotes()
                    # Step 3: aggressive scan
                    await self._scan_once()
                    log.info("Post-roll scan complete. Tracking %d markets.", len(self._kalshi._active_tickers))
                except Exception as exc:
                    log.warning("Contract roll handler error: %s", exc)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Kalshi Latency Arbitrage Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                          # paper trading (default, safe)
  python main.py --paper                  # explicit paper trading
  python main.py \\
    --enable-live-trading \\
    --confirm-live \\
    --override-live                       # live trading (use with caution)
        """,
    )
    p.add_argument("--paper", action="store_true", help="Force paper trading mode (default)")
    p.add_argument(
        "--enable-live-trading",
        action="store_true",
        help="Live trading flag 1/3 (must set all three to go live)",
    )
    p.add_argument(
        "--confirm-live",
        action="store_true",
        help="Live trading flag 2/3",
    )
    p.add_argument(
        "--override-live",
        action="store_true",
        help="Live trading flag 3/3",
    )
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Adjust log level from CLI
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    # CLI flags take precedence over .env when explicitly set
    if args.enable_live_trading:
        os.environ["LIVE_TRADING_ENABLE"] = "true"
    if args.confirm_live:
        os.environ["LIVE_TRADING_CONFIRM"] = "true"
    if args.override_live:
        os.environ["LIVE_TRADING_OVERRIDE"] = "true"
    if args.paper:
        # Force paper mode regardless of .env
        os.environ["LIVE_TRADING_ENABLE"] = "false"
        os.environ["LIVE_TRADING_CONFIRM"] = "false"
        os.environ["LIVE_TRADING_OVERRIDE"] = "false"

    cfg = Config.load()
    cfg.warn_paper_mode()

    if cfg.live.is_live:
        print(
            "\n⚠️  WARNING: LIVE TRADING ENABLED  ⚠️\n"
            "Real money will be at risk.  You have 5 seconds to abort (Ctrl-C)…\n"
        )
        import time
        time.sleep(5)

    bot = ArbBot(cfg)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    except Exception as exc:
        log.critical("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
