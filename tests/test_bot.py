import base64
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from config import config, KalshiConfig
from kalshi import KalshiClient, Market
from perplexity import PerplexityClient, MarketEstimate
from strategy import Strategy
from trader import Trader
from logger import BotLogger


def market(**changes):
    values = dict(ticker="TEST", title="Test", yes_price=60, no_price=45,
                  volume=10000, close_time=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat(), status="active")
    values.update(changes)
    return Market(**values)


class BotTests(unittest.TestCase):
    def test_signature_verifies_and_excludes_query(self):
        key = ed25519.Ed25519PrivateKey.generate()
        client = KalshiClient()
        client.api_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        client.api_key_id = "test"
        headers = client._sign("get", "/portfolio/balance?x=1")
        message = (headers["KALSHI-ACCESS-TIMESTAMP"] + "GET/trade-api/v2/portfolio/balance").encode()
        key.public_key().verify(base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]), message)

    def test_paper_default_and_environment(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(KalshiConfig().paper_trade)
        with patch.dict("os.environ", {"KALSHI_PAPER_TRADE": "false"}):
            self.assertFalse(KalshiConfig().paper_trade)

    def test_no_signal_uses_ask_and_correct_ev(self):
        estimate = MarketEstimate("TEST", .2, 9, "reason", "")
        signal = Strategy().evaluate(market(), estimate)
        self.assertEqual(signal.side, "no")
        self.assertAlmostEqual(signal.edge, .35)
        self.assertAlmostEqual(signal.expected_value, 35)
        self.assertLessEqual(signal.contracts, config.strategy.contracts_per_trade)

    def test_spread_does_not_manufacture_edge(self):
        self.assertIsNone(Strategy().evaluate(market(yes_price=60, no_price=60), MarketEstimate("TEST", .5, 9, "", "")))

    def test_invalid_prices_and_dates_skipped(self):
        self.assertEqual(Strategy().filter_markets([market(close_time="bad"), market(close_time="2030-01-01"), market(no_price=0)]), [])
        self.assertIsNone(Strategy().evaluate(market(yes_price=0), MarketEstimate("TEST", .9, 9, "", "")))

    def test_invalid_ai_values(self):
        client = PerplexityClient()
        for probability in ('"bad"', 'NaN', 'true', '1.5', 'null'):
            self.assertIsNone(client._parse_response("TEST", '{"probability":' + probability + ',"confidence":8}'))
        self.assertIsNone(client._parse_response("TEST", '{"probability":0.5,"confidence":7.5}'))

    def test_malformed_api_response_is_contained(self):
        response = Mock()
        response.json.return_value = {"choices": None}
        with patch("perplexity.requests.post", return_value=response):
            self.assertIsNone(PerplexityClient().estimate(market()))

    def test_market_normalization_pagination(self):
        client = KalshiClient()
        row = dict(ticker="A", yes_ask_dollars="0.6050", no_ask_dollars="0.4100", volume_24h_fp="12.00", status="active")
        client._request = Mock(side_effect=[{"markets": [row], "cursor": "next"}, {"markets": [{**row, "ticker": "B"}], "cursor": ""}])
        result = client.get_markets(page_size=1)
        self.assertEqual([m.ticker for m in result], ["A", "B"])
        self.assertEqual(result[0].yes_price, 61)
        self.assertEqual(result[0].volume, 12)

    def test_full_scan_excludes_combos_and_follows_empty_pages(self):
        client = KalshiClient()
        row = dict(ticker="LOW", yes_ask=50, no_ask=50, volume_24h=0,
                   status="active", close_time=market().close_time)
        client._request = Mock(side_effect=[
            {"markets": [row, {**row, "ticker": "KXMVE-COMBO"},
                         {**row, "ticker": "COMBO", "mve_collection_ticker": "COLLECTION"}], "cursor": "a"},
            {"markets": [], "cursor": "b"},
            {"markets": [{**row, "ticker": "HIGH", "volume_24h": 10000}, row], "cursor": ""},
        ])
        result = client.get_markets(page_size=2)
        self.assertEqual([m.ticker for m in result], ["LOW", "HIGH"])
        self.assertEqual(client._request.call_count, 3)
        for call in client._request.call_args_list:
            self.assertEqual(call.kwargs["params"]["mve_filter"], "exclude")
        with patch.object(config.strategy, "max_markets", 1):
            self.assertEqual([m.ticker for m in Strategy().filter_markets(result)], ["HIGH"])

    def test_market_fetch_cap_stops_and_sizes_final_page(self):
        client = KalshiClient()
        row = dict(ticker="A", yes_ask=50, no_ask=50, volume_24h=0)
        client._request = Mock(side_effect=[
            {"markets": [row, {**row, "ticker": "B"}], "cursor": "next"},
            {"markets": [{**row, "ticker": "C"}], "cursor": "more"},
        ])
        result = client.get_markets(page_size=2, max_markets=3)
        self.assertEqual(len(result), 3)
        self.assertEqual([c.kwargs["params"]["limit"] for c in client._request.call_args_list], [2, 1])

    def test_repeated_cursor_rejects_incomplete_scan(self):
        client = KalshiClient()
        client._request = Mock(return_value={"markets": [], "cursor": "same"})
        with self.assertRaisesRegex(ValueError, "full scan incomplete"):
            client.get_markets()
        self.assertEqual(client._request.call_count, 2)

    def test_paper_order_never_calls_network(self):
        client = KalshiClient()
        client._request = Mock()
        with patch.object(config.kalshi, "paper_trade", True):
            self.assertTrue(client.place_order("TEST", "yes", 1, 50)["paper"])
        client._request.assert_not_called()

    def test_live_no_order_uses_complement(self):
        client = KalshiClient()
        client._request = Mock(return_value={"order_id": "ack"})
        with patch.object(config.kalshi, "paper_trade", False):
            client.place_order("TEST", "no", 1, 40)
        payload = client._request.call_args.kwargs["json"]
        self.assertEqual(payload["side"], "ask")
        self.assertEqual(payload["price"], "0.6000")

    def test_order_limits_and_uncertain_attempts(self):
        client = KalshiClient()
        with self.assertRaises(ValueError):
            client.place_order("TEST", "yes", 0, 50)
        signal = Strategy().evaluate(market(), MarketEstimate("TEST", .9, 9, "", ""))
        client.place_order = Mock(side_effect=TimeoutError("unknown outcome"))
        trader = Trader(client)
        trader.execute_all([signal, signal])
        self.assertEqual(client.place_order.call_count, 1)

    def test_custom_paths_and_corrupt_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(config, "trades_file", str(root / "trades/log.csv")), patch.object(config, "evaluations_file", str(root / "eval/log.csv")), patch.object(config, "markets_file", str(root / "cache/markets.json")):
                logger = BotLogger()
                logger.save_markets_snapshot([market()])
                self.assertEqual(logger.load_markets_snapshot()[0]["ticker"], "TEST")
                logger.markets_file.write_text("broken")
                self.assertIsNone(logger.load_markets_snapshot())


if __name__ == "__main__":
    unittest.main()
