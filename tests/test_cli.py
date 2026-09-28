import argparse
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from apex_flow.cli import live, synthetic
from apex_flow.config import Config
from apex_flow.market import Market
from apex_flow.paper import Paper
from apex_flow.storage import Store


class ClosePositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_command_uses_fresh_book_when_market_stream_is_down(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / "paper.sqlite3"))
            try:
                config = Config(kill_file=str(Path(directory) / "KILL"))
                market = Market(config.symbol)
                market.connected = {"public", "market"}
                now = 1_800_000_000_000
                synthetic(market, store, now, 1)
                paper = Paper(config, store)
                paper.submit("initial-buy", "BUY", "0.5", now)
                synthetic(market, store, now + 101, 2)
                paper.step(market, now + 101)
                self.assertEqual(Decimal(paper.account["quantity"]), Decimal("0.5"))

                async def fake_collector(config, market, store, on_event, duration):
                    market.connected = {"public"}
                    synthetic(market, store, now + 200, 3)
                    market.disconnect("market")
                    on_event(now + 200)
                    synthetic(market, store, now + 301, 4)
                    on_event(now + 301)

                args = argparse.Namespace(command="paper", exercise_quantity=None,
                                          close_position=True, hold_seconds=10, seconds=1)
                with patch("apex_flow.collector.run_collector", fake_collector):
                    await live(config, store, args)
                self.assertEqual(Decimal(store.get("account")["quantity"]), 0)
            finally:
                store.close()
