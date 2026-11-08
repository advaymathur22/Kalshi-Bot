# Kalshi AI Trading Bot

An end-to-end prediction market trading bot that pulls Kalshi markets, estimates probabilities with Perplexity AI, calculates edge, and places (paper or real) trades automatically.

## Quickstart

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Set credentials

```bash
export KALSHI_API_KEY="your-private-key-pem"
export KALSHI_API_KEY_ID="your-key-id"
export PERPLEXITY_API_KEY="your-perplexity-key"
```

Or create a `.env` file in the project directory; it is loaded automatically.
Requires Python 3.10 or newer.

### 3. Run in paper trade mode (safe default)

```bash
python main.py
```

### 4. Other modes

```bash
python main.py --dry-run     # evaluate markets, no orders at all
python main.py --offline     # evaluate cached markets; never place orders
python main.py --summary     # print trade log and exit
python main.py --log-level DEBUG  # verbose output
```

---

## Architecture

```
main.py          — Entry point, orchestrates the full pipeline
config.py        — All settings (thresholds, API URLs, trade size)
kalshi.py        — Kalshi API client (market data + order placement)
perplexity.py    — Perplexity AI client (probability estimation)
strategy.py      — Market filtering + edge calculation
trader.py        — Order execution with safety limits
logger.py        — CSV + JSON logging

data/
  markets.json   — Snapshot of last fetched markets
  trades.csv     — Every trade placed (with outcome column for tracking)
  evaluations.csv— Every market evaluated (including skipped ones)

prompts/
  evaluate_market.txt  — Prompt template sent to Perplexity
```

## Configuration

Edit `config.py` to tune the strategy:

| Setting | Default | Description |
|---------|---------|-------------|
| `edge_threshold` | 0.001 | Minimum AI-vs-market edge to trade |
| `min_confidence` | 6 | Minimum Perplexity confidence score (1–10) |
| `min_volume` | 1 | Minimum 24h volume for liquidity |
| `max_markets` | 200 | Markets evaluated per run |
| `max_trades_per_run` | 3 | Safety cap on orders per run |
| `contracts_per_trade` | 1 | Contracts per order (start here!) |
| `paper_trade` | True | Simulate orders without real money |

## Going live

1. Create a Kalshi account and generate an API key
2. Set `KALSHI_BASE_URL=https://external-api.kalshi.com/trade-api/v2`
3. Set `paper_trade = False` in `config.py` (or set `KALSHI_PAPER_TRADE=false`)
4. Fund your account
5. Run with `--dry-run` a few times to verify signals look reasonable
6. Remove `--dry-run` — bot places real orders

## Tracking performance

Every trade is logged to `data/trades.csv`. After markets resolve:
1. Look up the outcome on Kalshi
2. Fill in the `outcome` column (`YES` or `NO`) and `pnl_cents`
3. Use `python main.py --summary` to review

The `data/evaluations.csv` logs *all* markets evaluated (including skips), so you can measure how accurate the AI estimates were over time — even for markets you didn't trade.

## Cron scheduling

Run the bot automatically every 6 hours:

```bash
0 */6 * * * cd /path/to/kalshi-bot && python main.py >> logs/bot.log 2>&1
```

## Verification

Run `python -m unittest discover -s tests -v`. Tests mock external APIs and place no orders.

Offline mode still calls Perplexity and requires its API key. Evaluation logs label candidate signals as `signal`; actual orders appear in the trade log. Expected value excludes exchange fees. The spend cap covers contract prices only.

Market discovery fetches up to 10,000 open market records in pages of up to 1,000 with `mve_filter=exclude`. After volume, price, and closing-time filters, candidates are sorted by 24-hour volume and limited to `max_markets` for AI evaluation. Run without `--offline` to refresh an older cache.
