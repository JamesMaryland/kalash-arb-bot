"""
Arbitrage + TA combined signal engine.

Signal tiers (in order of conviction):

  COMBINED  — Kalshi lags CEX price (arb) AND TA confirms same direction
              → full Kelly sizing (highest conviction)

  ARB_ONLY  — Kalshi lags CEX price but TA is neutral/disagrees
              → half Kelly sizing (structural edge, less directional certainty)

  TA_ONLY   — TA signals a clear direction but no Kalshi lag detected
              → half Kelly sizing (directional edge, no structural mispricing)

  NONE      — Neither signal fires → no trade

Pipeline:
  1. Check arb: does Kalshi lag Binance by > lag_threshold?
  2. Check TA: do RSI + EMA crossover + momentum agree on direction?
  3. Determine signal tier and apply matching Kelly multiplier.
  4. Gate: confidence > min_confidence AND position < max_position_pct.
  5. Return ArbOpportunity with signal_type embedded.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from bot.binance_feed import AssetState
from bot.config import RiskConfig
from bot.kalshi_client import MarketQuote
from bot.ta_engine import TAEngine, TASignal

log = logging.getLogger(__name__)


class SignalType(str, Enum):
    COMBINED = "COMBINED"   # arb + TA agree  — full Kelly
    ARB_ONLY = "ARB_ONLY"   # arb only        — half Kelly
    TA_ONLY  = "TA_ONLY"    # TA only         — half Kelly
    NONE     = "NONE"       # no trade


@dataclass
class ArbOpportunity:
    quote: MarketQuote
    side: str                 # "YES" or "NO"
    fair_prob: float          # estimated fair probability for YES
    kalshi_prob: float        # Kalshi's implied YES probability
    edge: float               # expected edge (0–1)
    confidence: float         # 0–1 score
    kelly_size_usd: float     # recommended position in USD
    max_contracts: int        # integer number of $1 contracts
    entry_price: float        # price per contract (0–1)
    signal_type: SignalType   # what fired this trade
    ta_signal: Optional[TASignal] = None

    @property
    def edge_pct(self) -> float:
        return self.edge

    def __str__(self) -> str:
        return (
            f"[{self.signal_type.value}] {self.quote.ticker} | side={self.side} "
            f"fair={self.fair_prob:.3f} kalshi={self.kalshi_prob:.3f} "
            f"edge={self.edge:.1%} conf={self.confidence:.1%} "
            f"kelly=${self.kelly_size_usd:.2f} ({self.max_contracts} contracts)"
        )


class ArbEngine:
    """
    Combined arb + TA signal generator.
    Call `evaluate(quote, asset_state, portfolio_value)` to get an
    ArbOpportunity or None.
    """

    def __init__(self, risk: RiskConfig) -> None:
        self._risk = risk
        self._ta = TAEngine()

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

        # ------------------------------------------------------------------
        # Signal detection: arb (Kalshi lag) and TA (technical indicators)
        # ------------------------------------------------------------------
        arb_fires = abs(delta) >= self._risk.lag_threshold_pct
        arb_side: Optional[str] = None
        if arb_fires:
            arb_side = "YES" if delta > 0 else "NO"

        ta_signal = self._ta.evaluate(asset_state)
        ta_fires = ta_signal.confirmed

        # ------------------------------------------------------------------
        # Signal tier selection
        # COMBINED  — arb fires AND TA confirms same direction → full Kelly
        # ARB_ONLY  — arb fires, TA neutral or disagrees       → half Kelly
        # TA_ONLY   — TA confirmed, arb below threshold        → half Kelly
        # NONE      — neither signal fires                     → no trade
        # ------------------------------------------------------------------
        if arb_fires and ta_fires and ta_signal.agrees_with(arb_side):
            signal_type = SignalType.COMBINED
            side = arb_side
            signal_kelly_mult = self._risk.combined_signal_kelly
        elif arb_fires:
            signal_type = SignalType.ARB_ONLY
            side = arb_side
            signal_kelly_mult = self._risk.single_signal_kelly
        elif ta_fires:
            signal_type = SignalType.TA_ONLY
            side = "YES" if ta_signal.direction == "UP" else "NO"
            signal_kelly_mult = self._risk.single_signal_kelly
        else:
            return None  # neither signal fired

        # ------------------------------------------------------------------
        # Side-specific entry price and fair probability
        # ------------------------------------------------------------------
        if side == "YES":
            entry_price = quote.yes_ask
            fair_prob_for_side = fair_yes_prob
        else:
            entry_price = quote.no_ask
            fair_prob_for_side = 1.0 - fair_yes_prob

        # Edge = fair probability for our side minus price paid
        edge = fair_prob_for_side - entry_price
        if edge <= 0:
            return None  # ask has moved past our fair value

        if edge < self._risk.min_edge_pct:
            log.debug(
                "Edge %.1f%% below minimum %.1f%% for %s [%s]",
                edge * 100, self._risk.min_edge_pct * 100, quote.ticker, signal_type.value,
            )
            return None

        # ------------------------------------------------------------------
        # Confidence score
        # ------------------------------------------------------------------
        confidence = self._score_confidence(quote, asset_state, edge)
        if confidence < self._risk.min_confidence:
            log.debug(
                "Confidence %.1f%% below minimum %.1f%% for %s [%s]",
                confidence * 100, self._risk.min_confidence * 100, quote.ticker, signal_type.value,
            )
            return None

        # ------------------------------------------------------------------
        # Kelly position sizing with signal-tier multiplier
        # ------------------------------------------------------------------
        kelly_size_usd = self._kelly_size(
            edge=edge,
            win_prob=fair_prob_for_side,
            entry_price=entry_price,
            portfolio_value=portfolio_value,
        )
        # Apply signal-tier fraction (COMBINED=1.0×, ARB_ONLY/TA_ONLY=0.5×)
        kelly_size_usd *= signal_kelly_mult

        # Hard cap: max_position_pct of portfolio
        max_allowed_usd = portfolio_value * self._risk.max_position_pct
        kelly_size_usd = min(kelly_size_usd, max_allowed_usd)

        if kelly_size_usd < 1.0:
            return None  # too small to be worth executing

        # Kalshi contracts pay $1 max; price == our max cost per contract
        max_contracts = max(1, int(kelly_size_usd / entry_price))

        log.debug(
            "%s signal: %s side=%s edge=%.1f%% conf=%.1f%% kelly_mult=%.1f kelly=$%.2f",
            signal_type.value, quote.ticker, side,
            edge * 100, confidence * 100, signal_kelly_mult, kelly_size_usd,
        )

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
            signal_type=signal_type,
            ta_signal=ta_signal,
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

        # -- Volume quality -- (threshold lowered to 50 so demo markets aren't penalised)
        vol_q = min(1.0, quote.volume / 50) if quote.volume else 0.3
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
