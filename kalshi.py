"""
kalshi.py - Kalshi REST API client.

Handles authenticated requests, market fetching, and V2 order placement.
Keeps the interface expected by strategy.py and trader.py.
"""

import base64
import logging
import math
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from config import config

logger = logging.getLogger(__name__)


@dataclass
class Market:
    ticker: str
    title: str
    yes_price: int  # cents to buy YES
    no_price: int  # cents to buy NO
    volume: float
    close_time: str
    status: str
    liquidity_dollars: float = 0.0
    rules: str = ""

    @property
    def implied_prob(self) -> float:
        return self.yes_price / 100.0

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "title": self.title,
            "yes_price": self.yes_price,
            "no_price": self.no_price,
            "volume": self.volume,
            "liquidity_dollars": self.liquidity_dollars,
            "rules": self.rules,
            "close_time": self.close_time,
            "status": self.status,
            "implied_prob": self.implied_prob,
        }


class KalshiClient:
    """REST client for the Kalshi Trade API."""

    def __init__(self):
        self.base_url = config.kalshi.base_url.rstrip("/")
        self.api_key = config.kalshi.api_key
        self.api_key_id = config.kalshi.api_key_id
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self._private_key = None

    def _sign(self, method: str, path: str) -> Dict[str, str]:
        """Sign timestamp + HTTP method + full URL path (never the body)."""
        if not self.api_key or not self.api_key_id:
            raise ValueError("Set KALSHI_API_KEY and KALSHI_API_KEY_ID to authenticate")

        if self._private_key is None:
            pem = self.api_key.replace("\\n", "\n").encode("utf-8")
            self._private_key = serialization.load_pem_private_key(
                pem, password=None
            )

        ts = str(int(time.time() * 1000))
        full_path = urlsplit(self.base_url + path).path
        message = f"{ts}{method.upper()}{full_path}".encode("utf-8")

        if isinstance(self._private_key, ed25519.Ed25519PrivateKey):
            signature = self._private_key.sign(message)
        elif isinstance(self._private_key, rsa.RSAPrivateKey):
            signature = self._private_key.sign(
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=hashes.SHA256().digest_size,
                ),
                hashes.SHA256(),
            )
        else:
            raise ValueError("Unsupported Kalshi API private key type")

        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
        }

    def _request(self, method, path, *, authenticated=False, **kwargs):
        headers = self._sign(method, path) if authenticated else {}
        response = self.session.request(
            method, self.base_url + path, headers=headers, timeout=30, **kwargs
        )
        if response.status_code == 403:
            logger.error(
            "Kalshi rejected %s %s: %s",
            method, path, response.text[:2000],
        )
        if response.status_code in (401, 403):
            logger.error(
            "Kalshi rejected %s %s: %s",
            method, path, response.text[:2000],
            )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object from Kalshi")
        return data

    @staticmethod
    def _parse_market(raw):
        def price(side):
            value = raw.get(f"{side}_ask_dollars")
            if value is None:
                value = Decimal(str(raw.get(f"{side}_ask", 0))) / 100
            value = Decimal(str(value)) * 100
            if not value.is_finite() or not 0 <= value <= 100:
                raise ValueError("Invalid market price")
            # This bot supports whole-cent limits; never round an ask down.
            return int(value.to_integral_value(rounding="ROUND_CEILING"))

        volume = float(raw.get("volume_24h_fp", raw.get("volume_24h", 0)) or 0)
        if not math.isfinite(volume) or volume < 0:
            raise ValueError("Invalid market volume")
        ticker = raw["ticker"]
        if not isinstance(ticker, str) or not ticker:
            raise ValueError("Missing market ticker")
        return Market(
            ticker=ticker, title=raw.get("title") or ticker,
            yes_price=price("yes"), no_price=price("no"), volume=volume,
            close_time=raw.get("close_time") or "",
            status=raw.get("status", ""),
            rules="\n".join(filter(None, [raw.get("rules_primary"), raw.get("rules_secondary")])),
        )

    def get_markets(self, page_size=1000, max_markets=10000):
        """Scan up to max_markets API records, excluding multivariate markets."""
        if type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ValueError("page_size must be an integer from 1 to 1000")
        if type(max_markets) is not int or max_markets < 1:
            raise ValueError("max_markets must be a positive integer")
        fetched = 0
        markets, seen, cursors = [], set(), set()
        cursor = None
        while fetched < max_markets:
            request_limit = min(page_size, max_markets - fetched)
            params = {"limit": request_limit, "status": "open", "mve_filter": "exclude"}
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "/markets", params=params)
            rows = data.get("markets")
            if not isinstance(rows, list):
                raise ValueError("Missing markets array")
            rows = rows[:request_limit]
            fetched += len(rows)
            for raw in rows:
                try:
                    if raw.get("mve_collection_ticker") or raw.get("mve_selected_legs") or str(raw.get("ticker", "")).startswith("KXMVE"):
                        continue
                    market = self._parse_market(raw)
                except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
                    logger.warning("Skipping malformed market record")
                    continue
                if market.ticker not in seen:
                    seen.add(market.ticker)
                    markets.append(market)
            cursor = data.get("cursor")
            logger.info("Fetched market page: %d records; %d unique non-multivariate markets so far", len(rows), len(markets))
            if fetched >= max_markets:
                logger.info("Market fetch cap reached (%d records)", max_markets)
                break
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in cursors:
                raise ValueError("Invalid or repeated market cursor; full scan incomplete")
            cursors.add(cursor)
        return markets

    def place_order(self, ticker, side, count, limit_price):
        if not isinstance(ticker, str) or not ticker or side not in ("yes", "no"):
            raise ValueError("Valid ticker and yes/no side required")
        if type(count) is not int or count < 1:
            raise ValueError("count must be a positive integer")
        if type(limit_price) is not int or not 1 <= limit_price <= 99:
            raise ValueError("limit_price must be an integer from 1 to 99 cents")
        if count > config.strategy.contracts_per_trade or count * limit_price > config.strategy.max_trade_spend_cents:
            raise ValueError("Order exceeds configured sizing limits")
        if config.kalshi.paper_trade:
            return {"order_id": "paper-" + str(uuid4()), "status": "simulated",
                    "paper": True, "price": None}
        # NO purchases are asks on the single YES book, at the complementary price.
        price = limit_price if side == "yes" else 100 - limit_price
        body = {"ticker": ticker, "client_order_id": str(uuid4()),
                "side": "bid" if side == "yes" else "ask",
                "count": f"{count}.00", "price": f"{price / 100:.4f}",
                "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross"}
        # Never automatically retry an order with an uncertain outcome.
        order = self._request("POST", "/portfolio/events/orders", authenticated=True, json=body)
        if not order.get("order_id"):
            raise ValueError("Order response missing order_id; inspect account before retrying")
        return {**order, "status": order.get("status", "submitted"), "paper": False}
