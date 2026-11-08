"""
main.py — Kalshi AI trading bot entry point.

Run:
    python main.py             # full run (fetch + evaluate + trade)
    python main.py --dry-run   # evaluate only, no orders
    python main.py --summary   # print trade log and exit
    python main.py --offline   # use cached markets.json (no Kalshi API call)

Environment variables required:
    KALSHI_API_KEY       — your Kalshi private key (PEM format)
    KALSHI_API_KEY_ID    — your Kalshi key ID
    PERPLEXITY_API_KEY   — your Perplexity API key

Set KALSHI_PAPER_TRADE=false to send real orders (default: paper mode).
"""
from dotenv import load_dotenv
load_dotenv()

import argparse
import logging
import sys

import requests
from datetime import datetime, timezone

from config import config
from kalshi import KalshiClient
from perplexity import PerplexityClient
from strategy import Strategy
from trader import Trader
from logger import BotLogger, setup_logging

log = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Kalshi AI trading bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate markets but do not place any orders.",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print trade log summary and exit.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Load markets from cached markets.json instead of Kalshi API.",
    )
    parser.add_argument(
        "--log-level",
        default=config.log_level,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def validate_config(args=None):
    """Warn about missing credentials before doing any work."""
    issues = []

    needs_orders = not config.kalshi.paper_trade and not (args and (args.dry_run or args.offline))
    if needs_orders and not config.kalshi.api_key:
        issues.append("KALSHI_API_KEY not set")
    if needs_orders and not config.kalshi.api_key_id:
        issues.append("KALSHI_API_KEY_ID not set")
    if not config.perplexity.api_key:
        issues.append("PERPLEXITY_API_KEY not set")

    if issues:
        for issue in issues:
            log.warning(f"Config warning: {issue}")
        if issues:
            log.error("Missing credentials with paper_trade=False — aborting.")
            sys.exit(1)

    mode = "PAPER TRADE" if config.kalshi.paper_trade else "⚠️  LIVE TRADING"
    log.info(f"Mode: {mode}")
    log.info(f"Edge threshold: {config.strategy.edge_threshold:.0%}")
    log.info(f"Min confidence: {config.strategy.min_confidence}/10")
    log.info(f"Max trades/run: {config.strategy.max_trades_per_run}")


def run(args):
    """Main trading loop."""
    start_time = datetime.now(timezone.utc)
    log.info("=" * 60)
    log.info(f"  Kalshi Bot starting — {start_time.strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 60)

    validate_config(args)

    # Initialize components
    kalshi = KalshiClient()
    perplexity = PerplexityClient()
    strategy = Strategy()
    trader = Trader(kalshi)
    bot_logger = BotLogger()

    # ------------------------------------------------------------------ #
    # Step 1 — Pull markets                                               #
    # ------------------------------------------------------------------ #
    if args.offline:
        log.info("Loading markets from cache…")
        raw = bot_logger.load_markets_snapshot()
        if not raw:
            log.error("No cached markets found. Run without --offline first.")
            sys.exit(1)

        # Reconstruct Market objects from dict
        from kalshi import Market
        all_markets = [
            Market(
                ticker=m["ticker"],
                title=m["title"],
                yes_price=m["yes_price"],
                no_price=m["no_price"],
                volume=m["volume"],
                rules=m["rules"],
                close_time=m["close_time"],
                status=m["status"],
                liquidity_dollars=m.get("liquidity_dollars", 0.0),
            )
            for m in raw
        ]
        log.info(f"Loaded {len(all_markets)} markets from cache.")
    else:
        all_markets = kalshi.get_markets()
        bot_logger.save_markets_snapshot(all_markets)

    if not all_markets:
        log.warning("No markets fetched. Exiting.")
        return

    # ------------------------------------------------------------------ #
    # Step 2 — Filter                                                     #
    # ------------------------------------------------------------------ #
    markets = strategy.filter_markets(all_markets)

    if not markets:
        log.warning("No markets passed filters. Try lowering min_volume.")
        return

    # ------------------------------------------------------------------ #
    # Step 3 — Ask Perplexity for probability estimates                  #
    # ------------------------------------------------------------------ #
    log.info(f"\nEvaluating {len(markets)} markets with Perplexity…\n")
    estimates = perplexity.estimate_batch(markets)

    # ------------------------------------------------------------------ #
    # Step 4 — Calculate edge and generate signals                       #
    # ------------------------------------------------------------------ #
    signals = []
    market_map = {m.ticker: m for m in markets}

    for ticker, estimate in estimates.items():
        market = market_map[ticker]
        signal = strategy.evaluate(market, estimate)
        bot_logger.log_evaluation(market, estimate, signal)

        if signal:
            signals.append(signal)

    if not signals:
        log.info("No signals above edge threshold this run.")
    else:
        signals = strategy.rank_signals(signals)
        log.info(f"\nFound {len(signals)} signal(s):\n")
        for s in signals:
            log.info(
                f"  {s.ticker:<24} {s.side.upper():<4} "
                f"edge={s.edge:+.2%}  EV={s.expected_value:+.1f}¢  "
                f"confidence={s.confidence}/10"
            )

    # ------------------------------------------------------------------ #
    # Step 5 — Place orders                                               #
    # ------------------------------------------------------------------ #
    if args.dry_run or args.offline:
        log.info("\n[DRY RUN] Skipping order placement.")
    else:
        trades = trader.execute_all(signals)
        for trade in trades:
            bot_logger.log_trade(trade)

        log.info(f"\n{trader.summary()}")

    # ------------------------------------------------------------------ #
    # Done                                                                #
    # ------------------------------------------------------------------ #
    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    log.info(f"\nRun complete in {elapsed:.1f}s")
    log.info(f"Markets evaluated : {len(estimates)}")
    log.info(f"Signals found     : {len(signals)}")
    log.info(f"Trades placed     : {trader.trade_count}")


def main():
    args = parse_args()
    setup_logging(args.log_level)

    if args.summary:
        bot_logger = BotLogger()
        bot_logger.print_trade_summary()
        return

    try:
        run(args)
    except (ValueError, OSError, KeyError, TypeError, requests.RequestException) as exc:
        log.error("Run failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
