"""
Binance WebSocket price feed.

Subscribes to bookTicker streams for each tracked asset and maintains:
  • latest mid-price (best_bid + best_ask) / 2
  • rolling price history for momentum calculation
  • derived fair-value probability for short-term direction
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from bot.config import BINANCE_STREAMS, BINANCE_WS_URL, MOMENTUM_WINDOW_SECS

log = logging.getLogger(__name__)

# Number of price ticks to keep for momentum/volatility rolling window
_HISTORY_MAXLEN = 500


@dataclass
class PriceTick:
    price: float
    bid: float
    ask: float
    ts: float  # epoch seconds


@dataclass
class AssetState:
    asset: str
    history: Deque[PriceTick] = field(default_factory=lambda: deque(maxlen=_HISTORY_MAXLEN))
    last_tick: Optional[PriceTick] = None

    @property
    def mid_price(self) -> Optional[float]:
        return self.last_tick.price if self.last_tick else None

    def momentum(self, window_secs: float = MOMENTUM_WINDOW_SECS) -> Optional[float]:
        """
        Returns fractional price change over the last `window_secs` seconds.
        Positive → price trending up; negative → trending down.
        """
        if len(self.history) < 2:
            return None
        now = time.monotonic()
        cutoff = now - window_secs
        # Find oldest tick within window
        baseline: Optional[PriceTick] = None
        for tick in self.history:
            if tick.ts >= cutoff:
                baseline = tick
                break
        if baseline is None or self.last_tick is None:
            return None
        if baseline.price == 0:
            return None
        return (self.last_tick.price - baseline.price) / baseline.price

    def volatility(self, window_secs: float = MOMENTUM_WINDOW_SECS) -> Optional[float]:
        """
        Returns approximate 1-sigma volatility (std-dev of log-returns)
        over the rolling window.  Used for confidence scoring.
        """
        if len(self.history) < 5:
            return None
        now = time.monotonic()
        cutoff = now - window_secs
        prices = [t.price for t in self.history if t.ts >= cutoff]
        if len(prices) < 2:
            return None
        import math
        log_returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
        n = len(log_returns)
        mean = sum(log_returns) / n
        variance = sum((r - mean) ** 2 for r in log_returns) / n
        return math.sqrt(variance)

    def fair_prob_up(self) -> Optional[float]:
        """
        Estimates the probability that price will be higher at the next
        contract expiry given recent momentum and volatility.

        Model:
          base_prob = 0.50
          adjustment = clamp(momentum / (4 * vol), -0.30, 0.30)
          fair_prob = base_prob + adjustment

        A 4-sigma move tilts the probability by 30pp at most.
        """
        mom = self.momentum()
        vol = self.volatility()
        if mom is None:
            return 0.50
        if vol is None or vol == 0:
            # No volatility estimate — use a dampened linear adjustment
            adjustment = max(-0.25, min(0.25, mom * 10))
        else:
            z_score = mom / (4 * vol)
            adjustment = max(-0.30, min(0.30, z_score * 0.30))
        return 0.50 + adjustment


class BinanceFeed:
    """
    Async WebSocket feed that keeps real-time prices for BTC and ETH.
    Reconnects automatically with exponential back-off.
    """

    def __init__(self, on_price: Optional[Callable[[str, AssetState], None]] = None) -> None:
        self._states: dict[str, AssetState] = {
            asset: AssetState(asset=asset) for asset in BINANCE_STREAMS
        }
        self._on_price = on_price  # optional callback for each tick
        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    def states(self) -> dict[str, AssetState]:
        return self._states

    def get_state(self, asset: str) -> Optional[AssetState]:
        return self._states.get(asset.upper())

    def mid_price(self, asset: str) -> Optional[float]:
        s = self._states.get(asset.upper())
        return s.mid_price if s else None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_forever(), name="binance-feed")
        log.info("Binance feed task started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Binance feed stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_url(self) -> str:
        streams = "/".join(BINANCE_STREAMS.values())
        return f"{BINANCE_WS_URL}?streams={streams}"

    async def _run_forever(self) -> None:
        backoff = 1.0
        max_backoff = 60.0
        while self._running:
            try:
                await self._connect()
                backoff = 1.0  # reset on clean close
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("Binance WS error: %s — reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def _connect(self) -> None:
        url = self._build_url()
        log.info("Connecting to Binance: %s", url)
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            log.info("Binance WebSocket connected")
            async for raw in ws:
                if not self._running:
                    break
                try:
                    self._handle_message(raw)
                except Exception as exc:
                    log.debug("Error parsing Binance message: %s", exc)

    def _handle_message(self, raw: str) -> None:
        envelope = json.loads(raw)
        # Combined streams wrap data in {"stream": "...", "data": {...}}
        data = envelope.get("data", envelope)
        stream = envelope.get("stream", "")

        # bookTicker format: {"u":..., "s":"BTCUSDT", "b":"bid", "B":"bidqty", "a":"ask", "A":"askqty"}
        symbol: str = data.get("s", "").upper()
        bid_s = data.get("b")
        ask_s = data.get("a")
        if not bid_s or not ask_s:
            return

        bid = float(bid_s)
        ask = float(ask_s)
        mid = (bid + ask) / 2.0

        # Map symbol → asset key (handles both USDT and USD pairs)
        asset: Optional[str] = None
        for a, stream_name in BINANCE_STREAMS.items():
            base = stream_name.split("@")[0].upper()
            # strip USD or USDT suffix from the received symbol to get base asset
            normalised = symbol.replace("USDT", "").replace("USD", "")
            if base.replace("USDT", "").replace("USD", "") == normalised:
                asset = a
                break
        if asset is None:
            return

        tick = PriceTick(price=mid, bid=bid, ask=ask, ts=time.monotonic())
        state = self._states[asset]
        state.history.append(tick)
        state.last_tick = tick

        if self._on_price:
            try:
                self._on_price(asset, state)
            except Exception:
                pass
