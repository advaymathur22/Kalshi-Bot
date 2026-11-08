"""
config.py — Central configuration for the Kalshi trading bot.
Copy this file and fill in your credentials before running.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

def env_bool(name, default=True):
    value = os.getenv(name)
    if value is None:
        return default
    if value.lower() not in ("true", "false"):
        raise ValueError(f"{name} must be true or false")
    return value.lower() == "true"


@dataclass
class KalshiConfig:
    # --- Kalshi credentials ---
    # Get these from https://kalshi.com/account/api
    api_key: str = field(default_factory=lambda: os.getenv("KALSHI_API_KEY", ""))
    api_key_id: str = field(default_factory=lambda: os.getenv("KALSHI_API_KEY_ID", ""))

    # Base URL — switch to prod when ready
    base_url: str = field(default_factory=lambda: os.getenv("KALSHI_BASE_URL", "https://external-api.demo.kalshi.co/trade-api/v2"))
    # Production: "https://trading-api.kalshi.com/trade-api/v2"

    # --- Paper trading mode ---
    # Set to True to simulate trades without real money
    paper_trade: bool = False


@dataclass
class PerplexityConfig:
    api_key: str = field(default_factory=lambda: os.getenv("PERPLEXITY_API_KEY", ""))
    model: str = "sonar-pro"  # web-grounded Perplexity Sonar model
    base_url: str = "https://api.perplexity.ai"
    max_tokens: int = 512
    temperature: float = 0.2  # low temp for more consistent JSON


@dataclass
class StrategyConfig:
    max_trade_spend_cents: int = 20  # $1 per trade

    # Minimum edge (AI prob - market implied prob) to place a trade
    edge_threshold: float = 0.1

    # Minimum AI confidence score (1-10) to consider a trade
    min_confidence: int = 7

    # Minimum 24h volume to consider a market liquid enough
    min_volume: int = 1000

    # Maximum number of markets to evaluate per run
    max_markets: int = 100

    # Maximum number of trades to place per run (safety limit)
    max_trades_per_run: int = 10

    # Contract count per trade (start with 1 for MVP)
    contracts_per_trade: int = 1

    # Skip markets closing within this many hours
    min_hours_to_close: int = 1


@dataclass
class AppConfig:
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    perplexity: PerplexityConfig = field(default_factory=PerplexityConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)

    # File paths
    markets_file: str = str(ROOT / "data/markets.json")
    trades_file: str = str(ROOT / "data/trades.csv")
    evaluations_file: str = str(ROOT / "data/evaluations.csv")
    prompt_file: str = str(ROOT / "prompts/evaluate_market.txt")

    # Logging
    log_level: str = "INFO"



config = AppConfig()
