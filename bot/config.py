"""
Configuration management — loads from .env and validates all settings.
Three explicit boolean flags must ALL be true to enable live trading.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

# Load .env from repo root (one level above this file)
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"Required env var {key!r} is not set. Copy .env.example → .env and fill it in.")
    return val


def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).lower() in ("1", "true", "yes")


def _float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class KalshiConfig:
    api_key: str
    api_secret: str
    environment: Literal["demo", "prod"]

    @classmethod
    def from_env(cls) -> "KalshiConfig":
        env = os.getenv("KALSHI_API_ENV", "demo").lower()
        if env not in ("demo", "prod"):
            raise ValueError("KALSHI_API_ENV must be 'demo' or 'prod'")
        return cls(
            api_key=_require("KALSHI_API_KEY"),
            api_secret=_require("KALSHI_API_SECRET"),
            environment=env,  # type: ignore[arg-type]
        )

    @property
    def base_url(self) -> str:
        if self.environment == "prod":
            return "https://trading-api.kalshi.com/trade-api/v2"
        return "https://demo-api.kalshi.co/trade-api/v2"


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: str

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        return cls(
            bot_token=_require("TELEGRAM_BOT_TOKEN"),
            chat_id=_require("TELEGRAM_CHAT_ID"),
        )


@dataclass(frozen=True)
class RiskConfig:
    portfolio_balance: float
    max_position_pct: float
    kelly_fraction: float
    min_edge_pct: float
    min_confidence: float
    lag_threshold_pct: float
    daily_drawdown_limit: float

    @classmethod
    def from_env(cls) -> "RiskConfig":
        return cls(
            portfolio_balance=_float("PORTFOLIO_BALANCE", 10_000.0),
            max_position_pct=_float("MAX_POSITION_PCT", 0.08),
            kelly_fraction=_float("KELLY_FRACTION", 0.5),
            min_edge_pct=_float("MIN_EDGE_PCT", 0.03),
            min_confidence=_float("MIN_CONFIDENCE", 0.70),
            lag_threshold_pct=_float("LAG_THRESHOLD_PCT", 0.03),
            daily_drawdown_limit=_float("DAILY_DRAWDOWN_LIMIT", 0.20),
        )


@dataclass(frozen=True)
class LiveTradingConfig:
    """
    Three independent flags must ALL be explicitly set to true to enable
    live trading.  A single mis-set flag keeps the bot in paper mode.
    """
    enable: bool
    confirm: bool
    override: bool

    @classmethod
    def from_env(cls) -> "LiveTradingConfig":
        return cls(
            enable=_bool("LIVE_TRADING_ENABLE"),
            confirm=_bool("LIVE_TRADING_CONFIRM"),
            override=_bool("LIVE_TRADING_OVERRIDE"),
        )

    @property
    def is_live(self) -> bool:
        return self.enable and self.confirm and self.override


# Kalshi series ticker prefixes for BTC/ETH up/down contracts.
# KXBTCD = "BTC Up or Down" series; KXETHD = "ETH Up or Down" series.
# The bot filters these by duration tag (15M, 1H) at discovery time.
WATCHED_ASSETS: dict[str, list[str]] = {
    "BTC": ["KXBTC"],
    "ETH": ["KXETH"],
}

# Binance.US streams for mid-price (binance.com blocks US IPs — use binance.us)
BINANCE_STREAMS: dict[str, str] = {
    "BTC": "btcusd@bookTicker",
    "ETH": "ethusd@bookTicker",
}

BINANCE_WS_URL = "wss://stream.binance.us:9443/stream"

# Rolling window (seconds) for price-momentum calculation
MOMENTUM_WINDOW_SECS: int = 30

# Max age (seconds) of a Kalshi quote before we skip it as stale
KALSHI_QUOTE_MAX_AGE_SECS: float = 5.0

# Rate-limit: max Kalshi API requests per second
KALSHI_RATE_LIMIT_RPS: float = 5.0

# SQLite path
DB_PATH: Path = _ROOT / "trades.db"


@dataclass
class Config:
    kalshi: KalshiConfig
    telegram: TelegramConfig
    risk: RiskConfig
    live: LiveTradingConfig

    @classmethod
    def load(cls) -> "Config":
        return cls(
            kalshi=KalshiConfig.from_env(),
            telegram=TelegramConfig.from_env(),
            risk=RiskConfig.from_env(),
            live=LiveTradingConfig.from_env(),
        )

    def warn_paper_mode(self) -> None:
        if not self.live.is_live:
            print(
                "\n[PAPER TRADING MODE]  All trades are simulated.\n"
                "To enable live trading set LIVE_TRADING_ENABLE=true, "
                "LIVE_TRADING_CONFIRM=true, and LIVE_TRADING_OVERRIDE=true in .env\n"
            )
