"""
perplexity.py — Perplexity AI client.

Uses Perplexity's online models (with live web search) to estimate
the probability a Kalshi market resolves YES.
"""

import json
import math
import logging
import re
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import requests

from config import config

logger = logging.getLogger(__name__)


@dataclass
class MarketEstimate:
    ticker: str
    probability: float      # AI's estimated YES probability (0–1)
    confidence: int         # 1–10
    reason: str
    raw_response: str       # full text for debugging

    @property
    def is_reliable(self) -> bool:
        return self.confidence >= config.strategy.min_confidence

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "probability": self.probability,
            "confidence": self.confidence,
            "reason": self.reason,
        }


class PerplexityClient:
    """Client for Perplexity's chat completions API."""

    def __init__(self):
        self.api_key = config.perplexity.api_key
        self.model = config.perplexity.model
        self.base_url = config.perplexity.base_url
        self._prompt_template = self._load_prompt()

    def _load_prompt(self) -> str:
        path = Path(config.prompt_file)
        if path.exists():
            return path.read_text(encoding="utf-8")
        logger.warning(f"Prompt file not found at {config.prompt_file}, using default.")
        return DEFAULT_PROMPT

    def _build_prompt(self, market) -> str:
        """Interpolate market data into the prompt template."""
        return self._prompt_template.format(
            title=market.title,
            yes_price=market.yes_price,
            implied_prob=market.implied_prob * 100,
            rules=market.rules or "No specific rules provided.",
            ticker=market.ticker,
            volume=market.volume,
            close_time=market.close_time,
        )

    def estimate(self, market) -> Optional[MarketEstimate]:
        """
        Ask Perplexity to estimate the probability a market resolves YES.
        Returns None on failure.
        """
        try:
            prompt = self._build_prompt(market)
        except (KeyError, ValueError, IndexError) as exc:
            logger.error("Invalid prompt template: %s", exc)
            return None

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        body = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a precise quantitative analyst. "
                        "Always respond with valid JSON only. No markdown, no preamble."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "max_tokens": config.perplexity.max_tokens,
            "temperature": config.perplexity.temperature,
        }

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=body,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data["choices"][0]["message"]["content"].strip()

            logger.debug(f"[{market.ticker}] Raw Perplexity response: {raw}")
            return self._parse_response(market.ticker, raw)

        except requests.HTTPError as e:
            response_text = e.response.text if e.response is not None else ""
            logger.error(
                f"[{market.ticker}] Perplexity API error: {e} | "
                f"Response: {response_text[:500]}"
            )
            return None
        except requests.RequestException as e:
            logger.error(f"[{market.ticker}] Perplexity API error: {e}")
            return None
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
            logger.error(f"[{market.ticker}] Unexpected API response structure: {e}")
            return None

    def _parse_response(self, ticker: str, raw: str) -> Optional[MarketEstimate]:
        """Extract and validate JSON from the model's response."""
        # Strip markdown code fences if model wraps in them
        cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()

        # Find the first JSON object in the response
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            logger.warning(f"[{ticker}] No JSON found in response: {raw[:200]}")
            return None

        try:
            parsed = json.loads(match.group())
        except json.JSONDecodeError as e:
            logger.warning(f"[{ticker}] JSON parse error: {e} — raw: {raw[:200]}")
            return None

        # Validate required fields
        prob = parsed.get("probability")
        confidence = parsed.get("confidence")
        reason = parsed.get("reason", "No reason provided.")

        if prob is None or confidence is None:
            logger.warning(f"[{ticker}] Missing required fields in: {parsed}")
            return None

        # Clamp values to valid ranges
        try:
            if isinstance(prob, bool) or isinstance(confidence, bool):
                return None
            prob = float(prob)
            score = float(confidence)
            if not math.isfinite(prob) or not 0 <= prob <= 1:
                return None
            if not math.isfinite(score) or not score.is_integer() or not 1 <= score <= 10:
                return None
            confidence = int(score)
        except (TypeError, ValueError, OverflowError):
            return None

        return MarketEstimate(
            ticker=ticker,
            probability=prob,
            confidence=confidence,
            reason=str(reason),
            raw_response=raw,
        )

    def estimate_batch(self, markets: list, delay: float = 1.0) -> dict[str, MarketEstimate]:
        """
        Estimate probabilities for multiple markets.
        Returns a dict of ticker → MarketEstimate.
        Adds a small delay between requests to avoid rate limiting.
        """
        results = {}
        total = len(markets)

        for i, market in enumerate(markets, 1):
            logger.info(f"Estimating [{i}/{total}]: {market.ticker} — {market.title[:60]}")
            estimate = self.estimate(market)
            if estimate:
                results[market.ticker] = estimate
            if i < total:
                time.sleep(delay)

        logger.info(f"Got estimates for {len(results)}/{total} markets.")
        return results


# Fallback if prompt file isn't found
DEFAULT_PROMPT = """
You are evaluating a Kalshi prediction market. 
Use the most current information from the web to base your decision off

Market: {title}
YES Price: {yes_price} cents (implies {implied_prob:.1f}% probability)
Rules: {rules}

Estimate:
- Probability YES resolves (0-1 float)
- Confidence in your estimate (1-10)
- Brief reason

Return ONLY valid JSON:
{{"probability": 0.XX, "confidence": X, "reason": "..."}}
""".strip()
