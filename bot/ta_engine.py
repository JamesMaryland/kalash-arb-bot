"""
Technical Analysis engine.

Uses the rolling tick history already collected by BinanceFeed to compute:
  • RSI (14-period)
  • EMA crossover (9 / 21-period)
  • Price momentum (already in AssetState)

Samples the tick history into 1-minute pseudo-candles so indicators
behave like they would on real OHLC data, then returns a TASignal
with a direction (UP / DOWN / NEUTRAL) and a confirmed flag.

Signal is considered confirmed when at least 2 of the 3 indicators agree.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from bot.binance_feed import AssetState

log = logging.getLogger(__name__)

# Minimum number of price samples required before we trust the indicators
_MIN_SAMPLES = 30

# RSI boundaries
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD   = 30.0

# How many seconds each pseudo-candle represents
CANDLE_SECS = 60


@dataclass
class TASignal:
    asset: str
    direction: str        # "UP", "DOWN", or "NEUTRAL"
    confirmed: bool       # True if ≥2 indicators agree on direction
    rsi: float            # 0–100
    ema_cross: str        # "BULLISH", "BEARISH", or "NEUTRAL"
    momentum_dir: str     # "UP", "DOWN", or "NEUTRAL"
    strength: float       # 0–1 aggregate conviction score

    def agrees_with(self, side: str) -> bool:
        """True if this TA signal agrees with the trade side (YES=UP, NO=DOWN)."""
        if not self.confirmed:
            return False
        if side == "YES":
            return self.direction == "UP"
        if side == "NO":
            return self.direction == "DOWN"
        return False


class TAEngine:
    """
    Stateless TA calculator.  Call evaluate(asset_state) to get a TASignal.
    """

    def evaluate(self, asset_state: AssetState) -> TASignal:
        asset = asset_state.asset
        prices = self._sample_prices(asset_state)

        if len(prices) < _MIN_SAMPLES:
            return TASignal(
                asset=asset,
                direction="NEUTRAL",
                confirmed=False,
                rsi=50.0,
                ema_cross="NEUTRAL",
                momentum_dir="NEUTRAL",
                strength=0.0,
            )

        rsi = self._rsi(prices)
        ema9 = self._ema(prices, 9)
        ema21 = self._ema(prices, 21)
        momentum = asset_state.momentum()

        # --- Direction votes ---
        votes_up   = 0
        votes_down = 0

        # RSI vote
        if rsi < RSI_OVERSOLD:
            votes_up += 1      # oversold → likely bounce up
        elif rsi > RSI_OVERBOUGHT:
            votes_down += 1    # overbought → likely pull down

        # EMA crossover vote
        if ema9 > ema21:
            ema_cross = "BULLISH"
            votes_up += 1
        elif ema9 < ema21:
            ema_cross = "BEARISH"
            votes_down += 1
        else:
            ema_cross = "NEUTRAL"

        # Momentum vote
        if momentum is not None and momentum > 0.0002:   # +0.02% threshold
            mom_dir = "UP"
            votes_up += 1
        elif momentum is not None and momentum < -0.0002:
            mom_dir = "DOWN"
            votes_down += 1
        else:
            mom_dir = "NEUTRAL"

        # --- Aggregate ---
        if votes_up >= 2:
            direction = "UP"
            confirmed = True
        elif votes_down >= 2:
            direction = "DOWN"
            confirmed = True
        else:
            direction = "NEUTRAL"
            confirmed = False

        # Strength: fraction of max possible votes in winning direction
        max_votes = 3
        winning_votes = max(votes_up, votes_down)
        strength = round(winning_votes / max_votes, 3)

        signal = TASignal(
            asset=asset,
            direction=direction,
            confirmed=confirmed,
            rsi=round(rsi, 2),
            ema_cross=ema_cross,
            momentum_dir=mom_dir,
            strength=strength,
        )
        log.debug(
            "TA [%s] dir=%s confirmed=%s rsi=%.1f ema=%s mom=%s strength=%.2f",
            asset, direction, confirmed, rsi, ema_cross, mom_dir, strength,
        )
        return signal

    # ------------------------------------------------------------------
    # Indicator calculations
    # ------------------------------------------------------------------

    def _sample_prices(self, state: AssetState) -> list[float]:
        """
        Downsample tick history into 1-minute pseudo-candle close prices.
        Takes the most recent tick within each 60-second bucket.
        """
        if not state.history:
            return []
        now = time.monotonic()
        buckets: dict[int, float] = {}
        for tick in state.history:
            bucket = int((now - tick.ts) // CANDLE_SECS)
            # Keep the newest tick per bucket (lowest elapsed time wins)
            if bucket not in buckets:
                buckets[bucket] = tick.price
        # Sort oldest → newest (largest bucket index → smallest)
        sorted_prices = [buckets[k] for k in sorted(buckets.keys(), reverse=True)]
        return sorted_prices

    @staticmethod
    def _rsi(prices: list[float], period: int = 14) -> float:
        if len(prices) < period + 1:
            return 50.0
        gains, losses = [], []
        for i in range(1, len(prices)):
            diff = prices[i] - prices[i - 1]
            gains.append(max(diff, 0.0))
            losses.append(max(-diff, 0.0))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _ema(prices: list[float], period: int) -> float:
        if len(prices) < period:
            return prices[-1] if prices else 0.0
        k = 2.0 / (period + 1)
        ema = sum(prices[:period]) / period
        for price in prices[period:]:
            ema = price * k + ema * (1.0 - k)
        return ema
