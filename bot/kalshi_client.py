"""
Kalshi CLOB API client wrapper.

Responsibilities:
  • Discover active BTC/ETH 5-min and 15-min up/down markets
  • Poll orderbooks at a rate-limited cadence
  • Expose MarketQuote objects (best bid/ask, implied YES probability)
  • Submit market/limit orders (paper mode: no-op, live mode: real API call)

Uses py-clob-client under the hood; falls back to raw httpx requests where
the SDK doesn't provide the needed endpoint.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from bot.config import (
    KALSHI_QUOTE_MAX_AGE_SECS,
    KALSHI_RATE_LIMIT_RPS,
    WATCHED_ASSETS,
    KalshiConfig,
)

log = logging.getLogger(__name__)

# Contract-type tags we care about (Kalshi uses these in series tickers)
_DURATION_TAGS = {
    "5M": "5M",
    "15M": "15M",
}


@dataclass
class MarketQuote:
    ticker: str          # e.g. "KXBTC-5M-2024-01-15T12:30:00-T14.50"
    asset: str           # "BTC" or "ETH"
    duration: str        # "5M" or "15M"
    direction: str       # "UP" or "DOWN"
    yes_bid: float       # best bid for YES (0–1 scale; Kalshi uses cents, we normalise)
    yes_ask: float       # best ask for YES
    no_bid: float
    no_ask: float
    implied_yes_prob: float   # mid of yes_bid/yes_ask
    volume: int
    fetched_at: float = field(default_factory=time.monotonic)

    @property
    def is_stale(self) -> bool:
        return (time.monotonic() - self.fetched_at) > KALSHI_QUOTE_MAX_AGE_SECS

    @property
    def yes_mid(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2.0

    @property
    def spread(self) -> float:
        return self.yes_ask - self.yes_bid


class RateLimiter:
    """Token-bucket rate limiter."""

    def __init__(self, rps: float) -> None:
        self._rps = rps
        self._tokens = rps
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._rps, self._tokens + elapsed * self._rps)
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            wait = (1.0 - self._tokens) / self._rps
        await asyncio.sleep(wait)
        async with self._lock:
            self._tokens = max(0.0, self._tokens - 1.0)


class KalshiClient:
    """
    Async wrapper around the Kalshi CLOB API.

    Market discovery is done once at startup and refreshed every
    `refresh_interval` seconds.  Quotes are fetched on demand via
    `get_quotes()`, which respects the rate limiter.
    """

    def __init__(self, cfg: KalshiConfig) -> None:
        self._cfg = cfg
        self._rate_limiter = RateLimiter(KALSHI_RATE_LIMIT_RPS)
        self._quotes: dict[str, MarketQuote] = {}  # ticker → latest quote
        self._active_tickers: list[str] = []
        self._http: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=self._cfg.base_url,
            headers={
                "Authorization": f"Bearer {self._cfg.api_key}",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )
        await self._discover_markets()
        log.info("KalshiClient started — tracking %d markets", len(self._active_tickers))

    async def stop(self) -> None:
        if self._http:
            await self._http.aclose()
        log.info("KalshiClient stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def quotes(self) -> dict[str, MarketQuote]:
        return self._quotes

    async def refresh_quotes(self) -> None:
        """Fetch fresh orderbook quotes for all tracked markets."""
        tasks = [self._fetch_quote(ticker) for ticker in self._active_tickers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                log.debug("Quote fetch error: %s", r)

    async def place_order(
        self,
        ticker: str,
        side: str,        # "YES" or "NO"
        size: int,        # number of contracts (integer; 1 contract = $1 max payout)
        price: float,     # limit price in [0, 1]
        is_paper: bool,
    ) -> Optional[str]:
        """
        Place a limit order.  Returns order_id (or "PAPER-<ticker>" in paper mode).
        """
        if is_paper:
            order_id = f"PAPER-{ticker}-{int(time.time())}"
            log.info("[PAPER] Order: %s %s x%d @ %.4f", ticker, side, size, price)
            return order_id

        await self._rate_limiter.acquire()
        try:
            resp = await self._http.post(  # type: ignore[union-attr]
                "/orders",
                json={
                    "ticker": ticker,
                    "client_order_id": f"arb-{int(time.time() * 1000)}",
                    "side": side,
                    "action": "buy",
                    "count": size,
                    "type": "limit",
                    "yes_price": int(price * 100),  # Kalshi uses cent integers
                },
            )
            resp.raise_for_status()
            order_id = resp.json().get("order", {}).get("order_id")
            log.info("[LIVE] Order placed: %s %s x%d → id=%s", ticker, side, size, order_id)
            return order_id
        except Exception as exc:
            log.error("Failed to place order for %s: %s", ticker, exc)
            return None

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    async def _discover_markets(self) -> None:
        """Find active BTC/ETH 5-min and 15-min markets."""
        found: list[str] = []
        for asset, prefixes in WATCHED_ASSETS.items():
            for prefix in prefixes:
                tickers = await self._fetch_markets_for_prefix(prefix)
                found.extend(tickers)
                log.debug("Discovered %d markets for prefix %s", len(tickers), prefix)
        self._active_tickers = found
        if not found:
            log.warning(
                "No active Kalshi markets found for %s.  "
                "The bot will still run and wait for markets to open.",
                list(WATCHED_ASSETS.keys()),
            )

    async def _fetch_markets_for_prefix(self, series_ticker: str) -> list[str]:
        """
        Query /markets with series_ticker filter and return active tickers.
        Falls back to an empty list on error so discovery never crashes.
        """
        await self._rate_limiter.acquire()
        try:
            resp = await self._http.get(  # type: ignore[union-attr]
                "/markets",
                params={
                    "series_ticker": series_ticker,
                    "status": "open",
                    "limit": 100,
                },
            )
            resp.raise_for_status()
            markets = resp.json().get("markets", [])
            return [m["ticker"] for m in markets if m.get("ticker")]
        except Exception as exc:
            log.warning("Market discovery failed for %s: %s", series_ticker, exc)
            return []

    # ------------------------------------------------------------------
    # Quote fetching
    # ------------------------------------------------------------------

    async def _fetch_quote(self, ticker: str) -> None:
        await self._rate_limiter.acquire()
        backoff = 1.0
        for attempt in range(3):
            try:
                resp = await self._http.get(  # type: ignore[union-attr]
                    f"/markets/{ticker}/orderbook",
                    params={"depth": 5},
                )
                resp.raise_for_status()
                data = resp.json()
                quote = self._parse_orderbook(ticker, data)
                if quote:
                    self._quotes[ticker] = quote
                return
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise
            except Exception as exc:
                if attempt < 2:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                else:
                    raise

    def _parse_orderbook(self, ticker: str, data: dict) -> Optional[MarketQuote]:
        """
        Parse a Kalshi /orderbook response into a MarketQuote.

        Kalshi returns prices in integer cents (0–100).  We normalise to [0, 1].
        """
        ob = data.get("orderbook", data)  # some endpoints nest under "orderbook"
        yes_bids: list[list] = ob.get("yes", [])  # [[price_cents, size], ...]
        no_bids: list[list] = ob.get("no", [])

        if not yes_bids and not no_bids:
            return None

        # Best YES bid/ask
        yes_bid_cents = max((lvl[0] for lvl in yes_bids), default=0)
        # Best NO bid implies best YES ask (since YES + NO = 100 cents)
        no_bid_cents = max((lvl[0] for lvl in no_bids), default=0)
        yes_ask_cents = 100 - no_bid_cents if no_bid_cents else yes_bid_cents + 1

        yes_bid = yes_bid_cents / 100.0
        yes_ask = yes_ask_cents / 100.0
        no_bid = no_bid_cents / 100.0
        no_ask = (100 - yes_bid_cents) / 100.0

        # Derive asset / duration / direction from ticker naming convention
        # e.g. KXBTC-5M-T123456-UP or KXBTC-5M-2024-01-15T12:30:00-T50
        asset, duration, direction = self._parse_ticker_meta(ticker)

        volume = data.get("volume", 0)

        return MarketQuote(
            ticker=ticker,
            asset=asset,
            duration=duration,
            direction=direction,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            implied_yes_prob=(yes_bid + yes_ask) / 2.0,
            volume=volume,
        )

    @staticmethod
    def _parse_ticker_meta(ticker: str) -> tuple[str, str, str]:
        """Extract (asset, duration, direction) from a Kalshi ticker string."""
        upper = ticker.upper()
        asset = "BTC" if "BTC" in upper else "ETH" if "ETH" in upper else "UNKNOWN"
        duration = "5M" if "5M" in upper else "15M" if "15M" in upper else "UNKNOWN"
        direction = "DOWN" if "DOWN" in upper else "UP"
        return asset, duration, direction
