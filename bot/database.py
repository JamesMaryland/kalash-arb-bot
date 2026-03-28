"""
SQLite persistence layer.

Tables
------
trades          — every executed (paper or live) order
positions       — current open positions (updated on fill/close)
price_snapshots — rolling price history used for diagnostics
daily_stats     — per-day P&L and drawdown summary
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import AsyncIterator, Optional

import aiosqlite

log = logging.getLogger(__name__)


class Side(str, Enum):
    YES = "YES"
    NO = "NO"


class TradeStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


@dataclass
class Trade:
    market_ticker: str
    asset: str          # BTC or ETH
    contract_type: str  # 5M_UP, 5M_DOWN, 15M_UP, 15M_DOWN
    side: Side
    size: float         # number of contracts
    entry_price: float  # price paid per contract (0–1 scale)
    fair_value: float   # our estimated fair value at entry
    edge_pct: float     # (fair_value - entry_price) / entry_price
    confidence: float
    kelly_fraction: float
    is_paper: bool
    status: TradeStatus = TradeStatus.OPEN
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[datetime] = None
    order_id: Optional[str] = None
    id: Optional[int] = None


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            async with aiosqlite.connect(self._path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("PRAGMA foreign_keys=ON")
                yield db

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def initialise(self) -> None:
        async with self._conn() as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_ticker   TEXT    NOT NULL,
                    asset           TEXT    NOT NULL,
                    contract_type   TEXT    NOT NULL,
                    side            TEXT    NOT NULL,
                    size            REAL    NOT NULL,
                    entry_price     REAL    NOT NULL,
                    fair_value      REAL    NOT NULL,
                    edge_pct        REAL    NOT NULL,
                    confidence      REAL    NOT NULL,
                    kelly_fraction  REAL    NOT NULL,
                    is_paper        INTEGER NOT NULL DEFAULT 1,
                    status          TEXT    NOT NULL DEFAULT 'OPEN',
                    exit_price      REAL,
                    pnl             REAL,
                    opened_at       TEXT    NOT NULL,
                    closed_at       TEXT,
                    order_id        TEXT
                );

                CREATE TABLE IF NOT EXISTS positions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id        INTEGER NOT NULL REFERENCES trades(id),
                    market_ticker   TEXT    NOT NULL,
                    asset           TEXT    NOT NULL,
                    side            TEXT    NOT NULL,
                    size            REAL    NOT NULL,
                    entry_price     REAL    NOT NULL,
                    current_price   REAL,
                    unrealised_pnl  REAL,
                    opened_at       TEXT    NOT NULL,
                    updated_at      TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS price_snapshots (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset       TEXT    NOT NULL,
                    source      TEXT    NOT NULL,   -- 'binance' or 'kalshi'
                    price       REAL    NOT NULL,
                    bid         REAL,
                    ask         REAL,
                    ts          TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_stats (
                    day             TEXT    PRIMARY KEY,
                    starting_balance REAL   NOT NULL,
                    ending_balance  REAL,
                    gross_pnl       REAL    DEFAULT 0,
                    num_trades      INTEGER DEFAULT 0,
                    num_wins        INTEGER DEFAULT 0,
                    max_drawdown    REAL    DEFAULT 0,
                    peak_balance    REAL    NOT NULL
                );
            """)
            await db.commit()
        log.info("Database initialised at %s", self._path)

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    async def insert_trade(self, trade: Trade) -> int:
        async with self._conn() as db:
            cursor = await db.execute(
                """
                INSERT INTO trades
                    (market_ticker, asset, contract_type, side, size,
                     entry_price, fair_value, edge_pct, confidence,
                     kelly_fraction, is_paper, status, opened_at, order_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    trade.market_ticker, trade.asset, trade.contract_type,
                    trade.side.value, trade.size, trade.entry_price,
                    trade.fair_value, trade.edge_pct, trade.confidence,
                    trade.kelly_fraction, int(trade.is_paper),
                    trade.status.value,
                    trade.opened_at.isoformat(), trade.order_id,
                ),
            )
            trade_id = cursor.lastrowid
            await db.execute(
                """
                INSERT INTO positions
                    (trade_id, market_ticker, asset, side, size,
                     entry_price, current_price, unrealised_pnl,
                     opened_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    trade_id, trade.market_ticker, trade.asset,
                    trade.side.value, trade.size, trade.entry_price,
                    trade.entry_price, 0.0,
                    trade.opened_at.isoformat(), trade.opened_at.isoformat(),
                ),
            )
            await db.commit()
            return trade_id  # type: ignore[return-value]

    async def close_trade(self, trade_id: int, exit_price: float, pnl: float) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._conn() as db:
            await db.execute(
                """
                UPDATE trades
                SET status='CLOSED', exit_price=?, pnl=?, closed_at=?
                WHERE id=?
                """,
                (exit_price, pnl, now, trade_id),
            )
            await db.execute("DELETE FROM positions WHERE trade_id=?", (trade_id,))
            await db.commit()

    async def update_position_price(
        self, market_ticker: str, current_price: float, unrealised_pnl: float
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._conn() as db:
            await db.execute(
                """
                UPDATE positions
                SET current_price=?, unrealised_pnl=?, updated_at=?
                WHERE market_ticker=?
                """,
                (current_price, unrealised_pnl, now, market_ticker),
            )
            await db.commit()

    async def get_open_trades(self) -> list[dict]:
        async with self._conn() as db:
            cursor = await db.execute(
                "SELECT * FROM trades WHERE status='OPEN' ORDER BY opened_at DESC"
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_recent_trades(self, limit: int = 10) -> list[dict]:
        async with self._conn() as db:
            cursor = await db.execute(
                "SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_today_stats(self) -> Optional[dict]:
        today = date.today().isoformat()
        async with self._conn() as db:
            cursor = await db.execute(
                "SELECT * FROM daily_stats WHERE day=?", (today,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def upsert_daily_stats(
        self,
        starting_balance: float,
        ending_balance: float,
        gross_pnl: float,
        num_trades: int,
        num_wins: int,
        max_drawdown: float,
        peak_balance: float,
    ) -> None:
        today = date.today().isoformat()
        async with self._conn() as db:
            await db.execute(
                """
                INSERT INTO daily_stats
                    (day, starting_balance, ending_balance, gross_pnl,
                     num_trades, num_wins, max_drawdown, peak_balance)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(day) DO UPDATE SET
                    ending_balance=excluded.ending_balance,
                    gross_pnl=excluded.gross_pnl,
                    num_trades=excluded.num_trades,
                    num_wins=excluded.num_wins,
                    max_drawdown=excluded.max_drawdown,
                    peak_balance=excluded.peak_balance
                """,
                (
                    today, starting_balance, ending_balance, gross_pnl,
                    num_trades, num_wins, max_drawdown, peak_balance,
                ),
            )
            await db.commit()

    async def record_price(
        self,
        asset: str,
        source: str,
        price: float,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO price_snapshots (asset, source, price, bid, ask, ts) VALUES (?,?,?,?,?,?)",
                (asset, source, price, bid, ask, ts),
            )
            await db.commit()

    async def get_win_rate(self) -> tuple[int, int]:
        """Returns (wins, total_closed)."""
        async with self._conn() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) as total, SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins "
                "FROM trades WHERE status='CLOSED'"
            )
            row = await cursor.fetchone()
            if row:
                total = row["total"] or 0
                wins = row["wins"] or 0
                return int(wins), int(total)
            return 0, 0
