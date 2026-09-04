"""Unit & Integration tests for Stock Market Screen Analyzer."""

import os
import tempfile
import unittest
from datetime import datetime

from scanner.market_analyzer import (
    MarketAnalyzer,
    StockSnapshot,
    MarketAnalysisResult,
)
from storage.event_store import EventStore


class TestMarketAnalyzer(unittest.TestCase):

    def setUp(self):
        self.analyzer = MarketAnalyzer()
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.store = EventStore(db_path=self.temp_db.name)

    def tearDown(self):
        if os.path.exists(self.temp_db.name):
            try:
                os.remove(self.temp_db.name)
            except Exception:
                pass

    def test_stock_snapshot_validation(self):
        snap = StockSnapshot(
            symbol="RELIANCE",
            name="Reliance Industries Ltd",
            price=1322.0,
            change=19.5,
            change_percent=1.50,
            direction="UP",
            market_status="Market closed"
        )
        self.assertEqual(snap.symbol, "RELIANCE")
        self.assertEqual(snap.price, 1322.0)
        self.assertEqual(snap.direction, "UP")

    def test_market_direction_classification(self):
        # 1. UP
        up_snap = StockSnapshot(
            symbol="TCS",
            price=3850.0,
            change=45.0,
            change_percent=1.18,
            direction="UP"
        )
        self.assertEqual(up_snap.direction, "UP")

        # 2. DOWN
        down_snap = StockSnapshot(
            symbol="INFY",
            price=1420.0,
            change=-25.0,
            change_percent=-1.73,
            direction="DOWN"
        )
        self.assertEqual(down_snap.direction, "DOWN")

        # 3. FLAT
        flat_snap = StockSnapshot(
            symbol="HDFCBANK",
            price=1650.0,
            change=0.0,
            change_percent=0.0,
            direction="FLAT"
        )
        self.assertEqual(flat_snap.direction, "FLAT")

    def test_event_store_market_snapshots_persistence(self):
        snaps = [
            StockSnapshot(symbol="RELIANCE", price=1322.0, change=19.5, change_percent=1.5, direction="UP"),
            StockSnapshot(symbol="TCS", price=3850.0, change=-20.0, change_percent=-0.52, direction="DOWN"),
            StockSnapshot(symbol="INFY", price=1500.0, change=0.0, change_percent=0.0, direction="FLAT"),
        ]
        inserted_count = self.store.save_market_snapshots_batch(snaps)
        self.assertEqual(inserted_count, 3)

        retrieved = self.store.get_latest_market_snapshots(limit=10)
        self.assertEqual(len(retrieved), 3)
        symbols = [r["symbol"] for r in retrieved]
        self.assertIn("RELIANCE", symbols)
        self.assertIn("TCS", symbols)
        self.assertIn("INFY", symbols)

        # Query history for specific symbol
        rel_history = self.store.get_symbol_history("RELIANCE")
        self.assertEqual(len(rel_history), 1)
        self.assertEqual(rel_history[0]["price"], 1322.0)
        self.assertEqual(rel_history[0]["direction"], "UP")

    def test_page_detection_heuristics(self):
        class MockPage:
            def __init__(self, url, title):
                self._url = url
                self._title = title
            def is_closed(self):
                return False
            @property
            def url(self):
                return self._url
            def title(self):
                return self._title

        pages = [
            MockPage("about:blank", "Blank Tab"),
            MockPage("https://accounts.google.com/signin", "Sign In"),
            MockPage("https://in.tradingview.com/symbols/NSE-RELIANCE/", "RELIANCE Share Price - NSE:RELIANCE - TradingView"),
        ]

        detected, page, reason = self.analyzer.detect_market_page(pages)
        self.assertTrue(detected)
        self.assertIsNotNone(page)
        self.assertIn("tradingview.com", page.url)

    def test_telegram_alert_cooldown_and_threshold(self):
        result_minor = MarketAnalysisResult(
            screen_detected=True,
            stocks_detected=2,
            top_gainers=[StockSnapshot(symbol="ABC", price=100.0, change=0.5, change_percent=0.5, direction="UP")],
            top_decliners=[]
        )
        # 0.5% does not breach default threshold of 3.0%
        self.assertFalse(self.analyzer.should_dispatch_telegram_alert(result_minor))

        result_major = MarketAnalysisResult(
            screen_detected=True,
            stocks_detected=2,
            top_gainers=[StockSnapshot(symbol="XYZ", price=200.0, change=8.0, change_percent=4.0, direction="UP")],
            top_decliners=[]
        )
        # 4.0% breaches 3.0%
        self.assertTrue(self.analyzer.should_dispatch_telegram_alert(result_major))
        # Cooldown should prevent immediate subsequent dispatch
        self.assertFalse(self.analyzer.should_dispatch_telegram_alert(result_major))


if __name__ == "__main__":
    unittest.main()
