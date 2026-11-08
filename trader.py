"""
trader.py — Trade execution layer.

Takes TradeSignals from strategy.py and places orders via the Kalshi client.
Enforces safety limits and tracks what was placed this session.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from config import config
from kalshi import KalshiClient
from strategy import TradeSignal

logger = logging.getLogger(__name__)


@dataclass
class PlacedTrade:
    signal: TradeSignal
    order_id: str
    status: str
    filled_price: Optional[int]
    paper: bool

    def to_dict(self) -> dict:
        d = self.signal.to_dict()
        d.update({
            "order_id": self.order_id,
            "order_status": self.status,
            "filled_price": self.filled_price,
            "paper": self.paper,
        })
        return d


class Trader:
    """Executes trade signals through the Kalshi API."""

    def __init__(self, client: KalshiClient):
        self.client = client
        self.placed: list[PlacedTrade] = []
        self._max_trades = config.strategy.max_trades_per_run
        self._attempts = 0
        self._tickers = set()

    def execute(self, signal: TradeSignal) -> Optional[PlacedTrade]:
        """
        Place one trade based on a TradeSignal.
        Returns PlacedTrade on success, None on failure.
        Enforces the per-run trade cap.
        """
        if signal.ticker in self._tickers:
            return None
        if self._attempts >= self._max_trades:
            logger.warning(
                f"Trade cap reached ({self._max_trades}). "
                f"Skipping {signal.ticker}."
            )
            return None

        logger.info(
            f"Executing: {signal.side.upper()} {signal.contracts}x "
            f"{signal.ticker} @ {signal.market_price}¢  "
            f"(edge={signal.edge:+.2%}, EV={signal.expected_value:+.1f}¢)"
        )

        self._attempts += 1
        self._tickers.add(signal.ticker)
        try:
            order = self.client.place_order(
                ticker=signal.ticker,
                side=signal.side,
                count=signal.contracts,
                limit_price=signal.market_price,
            )

            if not isinstance(order, dict) or not order.get("order_id"):
                raise ValueError("Missing order acknowledgement")

            trade = PlacedTrade(
                signal=signal,
                order_id=order.get("order_id", "unknown"),
                status=order.get("status", "unknown"),
                filled_price=order.get("price"),
                paper=order.get("paper", False),
            )
            self.placed.append(trade)

            mode = "📄 PAPER" if trade.paper else "🟢 LIVE"
            logger.info(
                f"{mode} trade placed — {trade.order_id} "
                f"({trade.status})"
            )
            return trade

        except Exception as e:
            logger.error(f"Order failed for {signal.ticker}: {e}")
            return None

    def execute_all(self, signals: list[TradeSignal]) -> list[PlacedTrade]:
        """Execute a list of signals in order, respecting the trade cap."""
        trades = []
        for signal in signals:
            trade = self.execute(signal)
            if trade:
                trades.append(trade)
        return trades

    @property
    def trade_count(self) -> int:
        return len(self.placed)

    def summary(self) -> str:
        if not self.placed:
            return "No trades placed this run."
        lines = [f"Trades placed: {len(self.placed)}"]
        for t in self.placed:
            s = t.signal
            mode = "[PAPER]" if t.paper else "[LIVE]"
            lines.append(
                f"  {mode} {s.side.upper()} {s.contracts}x {s.ticker} "
                f"@ {s.market_price}¢ | edge={s.edge:+.2%} | EV={s.expected_value:+.1f}¢"
            )
        return "\n".join(lines)
