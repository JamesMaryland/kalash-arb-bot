"""
Arbitrage opportunity detection and sizing engine.

Pipeline for each Kalshi market quote:
  1. Compare Kalshi implied probability to Binance-derived fair probability.
  2. If |delta| > lag_threshold (3pp default) → candidate opportunity.
  3. Score confidence based on: momentum strength, price recency,
     quote staleness, spread width, and volume.
  4. Gate: edge > min_edge (5%) AND confidence > min_confidence (85%)
     AND proposed position < max_position_pct (8%) of portfolio.
  5. Size with fractional Kelly Criterion (half-Kelly by default).
  6. Return ArbOpportunity if all gates pass, else None.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

from bot.binance_feed import AssetState
from bot.config import RiskConfig
from bot.kalshi_client import MarketQuote

log = logging.getLogger(__name__)


@dataclass
class ArbOpportunity:
    quote: MarketQuote
    side: str                 # "YES" or "NO" — which side we buy
    fair_prob: float          # our estimated fair probability for YES
    kalshi_prob: float        # Kalshi's implied YES probability
    edge: float               # |fair_prob - entry_price| — our expected edge
    confidence: float         # 0–1 score
    kelly_size_usd: float     # recommended position in USD
    max_contracts: int        # integer number of $1 contracts
    entry_price: float        # price we'd pay per contract (0–1)

    @property
    def edge_pct(self) -> float:
        return self.edge

    def __str__(self) -> str:
        return (
            f"ARB {self.quote.ticker} | side={self.side} "
            f"fair={self.fair_prob:.3f} kalshi={self.kalshi_prob:.3f} "
            f"edge={self.edge:.1%} conf={self.confidence:.1%} "
            f"kelly=${self.kelly_size_usd:.2f} ({self.max_contracts} contracts)"
        )


class ArbEngine:
    """
    Stateless signal generator.  Call `evaluate(quote, asset_state, portfolio_value)`
    to get an ArbOpportunity or None.
    """

    def __init__(self, risk: RiskConfig) -> None:
        self._risk = risk

    def evaluate(
        self,
        quote: MarketQuote,
        asset_state: AssetState,
        portfolio_value: float,
    ) -> Optional[ArbOpportunity]:
        if quote.is_stale:
            log.debug("Skipping stale quote: %s", quote.ticker)
            return None

        if quote.spread > 0.10:
            # Spread wider than 10 cents — too illiquid to trade reliably
            log.debug("Skipping wide-spread market: %s spread=%.2f", quote.ticker, quote.spread)
            return None

        # ------------------------------------------------------------------
        # Fair-value estimation
        # ------------------------------------------------------------------
        fair_yes_prob = asset_state.fair_prob_up()
        if fair_yes_prob is None:
            return None

        # For DOWN contracts the YES payout is on price going down
        if quote.direction == "DOWN":
            fair_yes_prob = 1.0 - fair_yes_prob

        kalshi_implied = quote.implied_yes_prob

        delta = fair_yes_prob - kalshi_implied  # positive → Kalshi under-pricing YES

        if abs(delta) < self._risk.lag_threshold_pct:
            return None  # lag is within tolerance, no opportunity

        # ------------------------------------------------------------------
        # Side selection: buy YES if we think YES is underpriced, else NO
        # ------------------------------------------------------------------
        if delta > 0:
            # YES is cheap relative to fair value
            side = "YES"
            entry_price = quote.yes_ask
            fair_prob_for_side = fair_yes_prob
        else:
            # NO is cheap
            side = "NO"
            entry_price = quote.no_ask
            fair_prob_for_side = 1.0 - fair_yes_prob

        # Edge = fair probability for our side minus price paid
        edge = fair_prob_for_side - entry_price
        if edge <= 0:
            return None  # ask has moved past our fair value

        if edge < self._risk.min_edge_pct:
            log.debug(
                "Edge %.1f%% below minimum %.1f%% for %s",
                edge * 100, self._risk.min_edge_pct * 100, quote.ticker,
            )
            return None

        # ------------------------------------------------------------------
        # Confidence score
        # ------------------------------------------------------------------
        confidence = self._score_confidence(quote, asset_state, edge)
        if confidence < self._risk.min_confidence:
            log.debug(
                "Confidence %.1f%% below minimum %.1f%% for %s",
                confidence * 100, self._risk.min_confidence * 100, quote.ticker,
            )
            return None

        # ------------------------------------------------------------------
        # Kelly position sizing
        # ------------------------------------------------------------------
        kelly_size_usd = self._kelly_size(
            edge=edge,
            win_prob=fair_prob_for_side,
            entry_price=entry_price,
            portfolio_value=portfolio_value,
        )

        # Hard cap: max_position_pct of portfolio
        max_allowed_usd = portfolio_value * self._risk.max_position_pct
        kelly_size_usd = min(kelly_size_usd, max_allowed_usd)

        if kelly_size_usd < 1.0:
            return None  # too small to be worth executing

        # Kalshi contracts pay $1 max; price == our max cost per contract
        max_contracts = max(1, int(kelly_size_usd / entry_price))

        return ArbOpportunity(
            quote=quote,
            side=side,
            fair_prob=fair_yes_prob,
            kalshi_prob=kalshi_implied,
            edge=edge,
            confidence=confidence,
            kelly_size_usd=kelly_size_usd,
            max_contracts=max_contracts,
            entry_price=entry_price,
        )

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------

    def _score_confidence(
        self,
        quote: MarketQuote,
        asset_state: AssetState,
        edge: float,
    ) -> float:
        """
        Aggregate confidence score in [0, 1].

        Components (weighted average):
          • momentum_strength  (30%) — how decisive is the price move
          • volatility_penalty (20%) — high vol → less certain of direction
          • edge_magnitude     (20%) — larger edge → more confident
          • spread_quality     (15%) — tighter spread → more confident
          • volume_quality     (15%) — more volume → more liquid
        """
        scores: list[tuple[float, float]] = []

        # -- Momentum strength --
        mom = asset_state.momentum()
        vol = asset_state.volatility()
        if mom is not None and vol is not None and vol > 0:
            z = abs(mom) / vol
            # Sigmoid mapping z-score to 0–1
            mom_score = 1.0 / (1.0 + math.exp(-0.5 * (z - 2.0)))
        elif mom is not None:
            mom_score = min(1.0, abs(mom) * 20)
        else:
            mom_score = 0.5
        scores.append((mom_score, 0.30))

        # -- Volatility penalty (lower vol = more directional certainty) --
        if vol is not None and vol > 0:
            # Normalise: 0.001 = low vol → 1.0, 0.01 = high vol → 0.0
            vol_score = max(0.0, 1.0 - (vol / 0.005))
        else:
            vol_score = 0.5
        scores.append((vol_score, 0.20))

        # -- Edge magnitude --
        # Map edge from [min_edge, 0.30] → [0.5, 1.0]
        min_e = self._risk.min_edge_pct
        edge_score = 0.5 + 0.5 * min(1.0, (edge - min_e) / max(0.001, 0.25 - min_e))
        scores.append((edge_score, 0.20))

        # -- Spread quality --
        spread_score = max(0.0, 1.0 - quote.spread / 0.10)
        scores.append((spread_score, 0.15))

        # -- Volume quality --
        vol_q = min(1.0, quote.volume / 1000) if quote.volume else 0.3
        scores.append((vol_q, 0.15))

        total_weight = sum(w for _, w in scores)
        confidence = sum(s * w for s, w in scores) / total_weight
        return round(confidence, 4)

    # ------------------------------------------------------------------
    # Kelly Criterion
    # ------------------------------------------------------------------

    def _kelly_size(
        self,
        edge: float,
        win_prob: float,
        entry_price: float,
        portfolio_value: float,
    ) -> float:
        """
        Full Kelly fraction for a binary bet, then apply fractional multiplier.

        For a binary contract:
          b = (1 - entry_price) / entry_price  (net odds on a win)
          p = win_prob
          q = 1 - win_prob
          f* = (b*p - q) / b = p - q/b

        Then apply kelly_fraction (0.5 for half-Kelly).
        """
        if entry_price <= 0 or entry_price >= 1:
            return 0.0
        b = (1.0 - entry_price) / entry_price  # net odds
        p = win_prob
        q = 1.0 - p
        if b == 0:
            return 0.0
        full_kelly_fraction = p - (q / b)
        if full_kelly_fraction <= 0:
            return 0.0
        fractional_kelly = full_kelly_fraction * self._risk.kelly_fraction
        return portfolio_value * fractional_kelly
