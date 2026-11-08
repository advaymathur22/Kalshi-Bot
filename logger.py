"""
logger.py — Logging and persistence layer.

Writes every evaluated market and placed trade to disk so you can
review performance, backtest, and measure whether the edge is real.
"""

import csv
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import config

log = logging.getLogger(__name__)

# CSV columns for the trade log
TRADE_COLUMNS = [
    "date",
    "ticker",
    "title",
    "side",
    "market_price",
    "ai_probability",
    "market_implied_prob",
    "edge",
    "expected_value",
    "confidence",
    "contracts",
    "order_id",
    "order_status",
    "paper",
    "reason",
    "outcome",          # filled in later when market resolves
    "pnl_cents",        # filled in later
]

# CSV columns for the market evaluation log (all markets, not just trades)
EVAL_COLUMNS = [
    "date",
    "ticker",
    "title",
    "yes_price",
    "ai_probability",
    "market_implied_prob",
    "edge",
    "expected_value",
    "confidence",
    "action_taken",     # "trade" or "skip"
    "reason",
]


class BotLogger:
    """Handles writing trade logs, evaluation logs, and market snapshots."""

    def __init__(self):
        self.trades_file = Path(config.trades_file)
        self.markets_file = Path(config.markets_file)
        self.eval_file = Path(config.evaluations_file)
        self._ensure_files()

    def _ensure_files(self):
        """Create data files with headers if they don't exist."""
        for path in (self.trades_file, self.eval_file, self.markets_file):
            path.parent.mkdir(parents=True, exist_ok=True)

        if not self.trades_file.exists() or self.trades_file.stat().st_size == 0:
            self._write_csv_header(self.trades_file, TRADE_COLUMNS)
            log.info(f"Created trade log: {self.trades_file}")

        if not self.eval_file.exists() or self.eval_file.stat().st_size == 0:
            self._write_csv_header(self.eval_file, EVAL_COLUMNS)
            log.info(f"Created evaluation log: {self.eval_file}")

    def _write_csv_header(self, path: Path, columns: list):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def log_trade(self, trade) -> None:
        """Append a placed trade to trades.csv."""
        row = {
            "date": self._now(),
            **trade.to_dict(),
            "outcome": "",
            "pnl_cents": "",
        }
        self._append_csv(self.trades_file, TRADE_COLUMNS, row)
        log.info(f"Logged trade: {trade.signal.ticker} [{trade.order_id}]")

    def log_evaluation(self, market, estimate, signal=None) -> None:
        """Log every evaluated market, whether traded or not."""
        row = {
            "date": self._now(),
            "ticker": market.ticker,
            "title": market.title,
            "yes_price": market.yes_price,
            "ai_probability": round(estimate.probability, 4),
            "market_implied_prob": round(market.implied_prob, 4),
            "edge": round(signal.edge if signal else estimate.probability - market.implied_prob, 4),
            "expected_value": round(signal.expected_value if signal else
                (estimate.probability * (100 - market.yes_price))
                - ((1 - estimate.probability) * market.yes_price),
                2,
            ),
            "confidence": estimate.confidence,
            "action_taken": "signal" if signal else "skip",
            "reason": estimate.reason[:200],  # cap length
        }
        self._append_csv(self.eval_file, EVAL_COLUMNS, row)

    def save_markets_snapshot(self, markets: list) -> None:
        """Save a JSON snapshot of the current market set."""
        data = {
            "timestamp": self._now(),
            "count": len(markets),
            "markets": [m.to_dict() for m in markets],
        }
        with open(self.markets_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log.info(f"Saved {len(markets)} markets to {self.markets_file}")

    def load_markets_snapshot(self) -> Optional[list]:
        """Load the last saved market snapshot (for offline testing)."""
        if not self.markets_file.exists():
            return None
        try:
            with open(self.markets_file, encoding="utf-8") as f:
                data = json.load(f)
            markets = data.get("markets", [])
            if not isinstance(markets, list):
                raise ValueError("markets must be an array")
            return markets
        except (OSError, ValueError, AttributeError) as exc:
            log.error("Cannot load market snapshot: %s", exc)
            return None

    def _append_csv(self, path: Path, columns: list, row: dict) -> None:
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writerow(row)

    # ------------------------------------------------------------------ #
    # Performance review helpers                                           #
    # ------------------------------------------------------------------ #

    def print_trade_summary(self) -> None:
        """Print a summary of all trades from the CSV."""
        if not self.trades_file.exists():
            print("No trades logged yet.")
            return

        trades = []
        with open(self.trades_file, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            trades = list(reader)

        if not trades:
            print("No trades logged yet.")
            return

        total = len(trades)
        paper = sum(1 for t in trades if t.get("paper") == "True")
        live = total - paper

        print(f"\n{'='*60}")
        print(f"  TRADE LOG SUMMARY  ({self.trades_file})")
        print(f"{'='*60}")
        print(f"  Total trades : {total}")
        print(f"  Paper trades : {paper}")
        print(f"  Live trades  : {live}")
        print(f"{'='*60}\n")

        for t in trades[-10:]:  # show last 10
            outcome = t.get("outcome") or "pending"
            print(
                f"  {t['date'][:10]}  {t['ticker']:<20} "
                f"{t['side'].upper()} @ {t['market_price']}¢  "
                f"edge={float(t['edge']):+.2%}  [{outcome}]"
            )
        print()


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger with timestamps and level."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quiet noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
