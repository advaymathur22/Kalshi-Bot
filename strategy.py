"""
strategy.py — Market filtering and edge calculation.

Decides which markets are worth evaluating and which have enough
edge to warrant placing a trade.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from config import config

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    ticker: str
    title: str
    side: str               # "yes" or "no"
    market_price: int       # cents
    ai_probability: float
    market_implied_prob: float
    edge: float             # advantage for the selected side
    confidence: int
    reason: str
    contracts: int = 1

    @property
    def expected_value(self) -> float:
        """
        Expected value of buying one YES contract at market price.
        EV = (AI_prob * payout_if_win) - cost
        Payout if win = 100 cents. Cost = yes_price cents.
        """
        probability = self.ai_probability if self.side == "yes" else 1 - self.ai_probability
        return probability * 100 - self.market_price

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "title": self.title,
            "side": self.side,
            "market_price": self.market_price,
            "ai_probability": round(self.ai_probability, 4),
            "market_implied_prob": round(self.market_implied_prob, 4),
            "edge": round(self.edge, 4),
            "expected_value": round(self.expected_value, 2),
            "confidence": self.confidence,
            "reason": self.reason,
            "contracts": self.contracts,
        }


class Strategy:
    """Filters markets and calculates trading signals."""

    def __init__(self):
        self.cfg = config.strategy

    # ------------------------------------------------------------------ #
    # Step 1 — Filter markets worth evaluating                            #
    # ------------------------------------------------------------------ #

    def filter_markets(self, markets: list) -> list:
        """
        Narrow down to the most promising markets for AI evaluation.
        Filters by volume, status, price, and time-to-close.
        """
        now = datetime.now(timezone.utc)
        min_close = now + timedelta(hours=self.cfg.min_hours_to_close)

        candidates = []
        skipped = {"volume": 0, "status": 0, "price": 0, "time": 0}

        for m in markets:
            # Must be open
            if m.ticker.startswith("KXMVE") or m.status not in ("open", "active"):
                skipped["status"] += 1
                continue

            # Must have enough volume to be liquid
            if m.volume < self.cfg.min_volume:
                skipped["volume"] += 1
                continue

            # Must have a real price (avoid extreme/illiquid markets)
            if not (5 <= m.yes_price <= 95 and 1 <= m.no_price <= 99):
                skipped["price"] += 1
                continue

            # Must not close too soon (not enough time to profit)
            if m.close_time:
                try:
                    close_dt = datetime.fromisoformat(
                        m.close_time.replace("Z", "+00:00")
                    )
                    if close_dt.tzinfo is None:
                        raise ValueError("Missing timezone")
                    if close_dt < min_close:
                        skipped["time"] += 1
                        continue
                except (ValueError, TypeError):
                    skipped["time"] += 1
                    continue
            else:
                skipped["time"] += 1
                continue

            candidates.append(m)

        logger.info(
            f"Filtered {len(markets)} → {len(candidates)} candidates. "
            f"Skipped: {skipped}"
        )

        # Sort by volume descending (most liquid first), then cap
        candidates.sort(key=lambda m: m.volume, reverse=True)
        result = candidates[: self.cfg.max_markets]

        logger.info(f"Evaluating top {len(result)} markets.")
        return result

    # ------------------------------------------------------------------ #
    # Step 2 — Calculate edge and generate signals                        #
    # ------------------------------------------------------------------ #

    def evaluate(self, market, estimate) -> Optional[TradeSignal]:
        """
        Compare AI probability to market-implied probability.
        Returns a TradeSignal if there's enough edge, else None.
        """
        # Bail if AI isn't confident enough
        if estimate.ticker != market.ticker or not math.isfinite(estimate.probability) or not 0 <= estimate.probability <= 1:
            return None
        if not (1 <= market.yes_price <= 99 and 1 <= market.no_price <= 99):
            return None
        if not estimate.is_reliable:
            logger.debug(
                f"[{market.ticker}] Skipping: low confidence ({estimate.confidence})"
            )
            return None

        market_prob = market.implied_prob
        ai_prob = estimate.probability

        # Edge on the YES side: AI thinks YES more likely than market
        yes_edge = ai_prob - market_prob
        # Edge on the NO side: AI thinks NO more likely than market
        no_edge = (1 - ai_prob) - market.no_price / 100

        best_edge = yes_edge
        side = "yes"

        if no_edge > yes_edge:
            best_edge = no_edge
            side = "no"

        if best_edge < self.cfg.edge_threshold:
            logger.debug(
                f"[{market.ticker}] No edge: {best_edge:+.2%} "
                f"(threshold {self.cfg.edge_threshold:.0%})"
            )
            return None

        price = market.yes_price if side == "yes" else market.no_price

        contracts = min(self.cfg.contracts_per_trade, self.cfg.max_trade_spend_cents // price)

        if contracts < 1:
            return None
        signal = TradeSignal(
            ticker=market.ticker,
            title=market.title,
            side=side,
            market_price=price,
            ai_probability=ai_prob,
            market_implied_prob=market_prob,
            edge=best_edge,
            confidence=estimate.confidence,
            reason=estimate.reason,
            contracts=contracts,
        )

        logger.info(
            f"[{market.ticker}] SIGNAL: {side.upper()} | "
            f"Edge={best_edge:+.2%} | EV={signal.expected_value:+.1f}¢ | "
            f"Confidence={estimate.confidence}/10"
        )
        return signal

    def rank_signals(self, signals: list[TradeSignal]) -> list[TradeSignal]:
        """Sort signals by expected value * confidence (quality-adjusted)."""
        return sorted(
            signals,
            key=lambda s: s.expected_value * s.confidence,
            reverse=True,
        )
