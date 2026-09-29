import json
from decimal import Decimal as D
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from apex_flow.config import Config
from apex_flow.storage import Store
from apex_flow.market import Market
from apex_flow.paper import Paper
from apex_flow.demo import synthetic, point


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(kill_file=str(Path(self.tmp.name)/"KILL"))
        self.store = Store(str(Path(self.tmp.name)/"test.sqlite"))
        self.paper = Paper(self.config, self.store)
        self.market = Market("BTCUSDT")
        self.market.connected = {"public", "market"}
        self.t = 1_800_000_000_000
        synthetic(self.market, self.store, self.t, 1)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def advance(self, bid="100", ask="100.02"):
        self.t += 101
        synthetic(self.market, self.store, self.t, self.market.update_id+1, bid, ask)

    def buy(self, quantity="0.5"):
        self.paper.submit("buy", "BUY", quantity, point(self.t))
        self.advance()
        return self.paper.step(self.market, point(self.t))

    def test_round_trip_costs_and_accounting(self):
        result = self.buy()
        self.assertEqual(result["status"], "FILLED")
        self.paper.submit("sell", "SELL", "0.5", point(self.t), True)
        self.advance()
        self.paper.step(self.market, point(self.t))
        self.assertEqual(D(self.paper.account["quantity"]), 0)
        self.assertLess(D(self.paper.account["cash"]), self.config.initial_cash)
        self.assertEqual(D(self.paper.account["cash"])-self.config.initial_cash, D(self.paper.account["realized"]))

    def test_idempotent_and_conflicting_id(self):
        result = self.buy()
        cash = self.paper.account["cash"]
        self.assertEqual(result, self.paper.submit("buy", "BUY", "0.5", point(self.t)))
        self.assertEqual(cash, self.paper.account["cash"])
        with self.assertRaises(ValueError):
            self.paper.submit("buy", "BUY", "0.6", point(self.t))

    def test_latency_and_future_book(self):
        self.paper.submit("buy", "BUY", "0.5", point(self.t))
        self.assertIsNone(self.paper.step(self.market, point(self.t)))
        self.assertIsNone(self.paper.step(self.market, point(self.t+100)))
        self.assertEqual(D(self.paper.account["quantity"]), 0)
        self.advance()
        self.assertEqual(self.paper.step(self.market, point(self.t))["status"], "FILLED")

    def test_partial_fill_cancels_remainder(self):
        self.paper.submit("buy", "BUY", "0.5", point(self.t))
        self.advance()
        self.market.asks = [(D("100.02"), D("0.1"))]
        result = self.paper.step(self.market, point(self.t))
        self.assertEqual(result["status"], "PARTIAL_CANCELLED")
        self.assertEqual(D(self.paper.account["quantity"]), D("0.1"))
        self.assertIsNone(self.paper.pending)

    def test_disconnect_rejects(self):
        self.paper.submit("buy", "BUY", "0.5", point(self.t))
        self.advance()
        self.market.disconnect("market")
        self.assertEqual(self.paper.step(self.market, point(self.t))["status"], "REJECTED")

    def test_stale_exchange_timestamp_rejects_even_recent_receipt(self):
        self.market.last["aggTrade"] = (point(self.t), self.t-3000)
        self.assertFalse(self.market.healthy(point(self.t), 2000))

    def test_kill_blocks_entry_but_allows_reduce(self):
        self.buy()
        Path(self.config.kill_file).touch()
        self.paper.submit("buy2", "BUY", "0.1", point(self.t))
        self.advance()
        self.assertEqual(self.paper.step(self.market, point(self.t))["status"], "REJECTED")
        self.paper.submit("sell", "SELL", "10", point(self.t), True)
        self.advance()
        self.paper.step(self.market, point(self.t))
        self.assertEqual(D(self.paper.account["quantity"]), 0)

    def test_exposure_limit(self):
        self.assertEqual(self.buy("2")["status"], "REJECTED")

    def test_restart_restores_account_cancels_pending(self):
        self.buy()
        self.paper.submit("pending", "BUY", "0.1", point(self.t))
        expected = dict(self.paper.account)
        self.store.close()
        self.store = Store(str(Path(self.tmp.name)/"test.sqlite"))
        paper = Paper(self.config, self.store)
        self.assertEqual(paper.account, expected)
        self.assertEqual(self.store.order("pending")["status"], "CANCELLED")
        self.assertIsNone(paper.pending)

    def test_daily_loss_includes_unrealized(self):
        self.buy()
        self.advance("50", "50.01")
        self.paper.submit("again", "BUY", "0.1", point(self.t))
        self.advance("50", "50.01")
        self.assertEqual(self.paper.step(self.market, point(self.t))["status"], "REJECTED")

    def test_old_book_does_not_overwrite_or_refresh(self):
        self.advance()
        before = self.market.last["depthUpdate"]
        self.assertFalse(self.market.apply("btcusdt@depth20@100ms", dict(e="depthUpdate", E=self.t+1,s="BTCUSDT",u=1,b=[["1","1"]],a=[["2","1"]]),point(self.t+1)))
        self.assertEqual(before, self.market.last["depthUpdate"])

    def test_diff_depth_rejected(self):
        with self.assertRaises(ValueError):
            self.market.apply("btcusdt@depth@100ms",dict(e="depthUpdate",E=self.t,s="BTCUSDT",u=2),point(self.t))

    def test_invalid_config_and_quantity(self):
        for value in ("NaN", "Infinity", "-1"):
            with self.assertRaises(ValueError):
                replace(self.config, max_notional=D(value))
            with self.assertRaises(ValueError):
                self.paper.submit("bad", "BUY", value, point(self.t))

    def test_symbol_is_bound_to_account(self):
        with self.assertRaises(ValueError):
            Paper(replace(self.config, symbol="ETHUSDT"), self.store)

    def test_db_lock_prevents_multiple_engines(self):
        with self.assertRaises(RuntimeError):
            Store(str(Path(self.tmp.name)/"test.sqlite"))

    def test_fill_transaction_rolls_back_on_write_failure(self):
        self.paper.submit("buy", "BUY", "0.5", point(self.t))
        self.advance()
        before = self.store.get("account")
        with patch.object(self.store, "save_order", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.paper.step(self.market, point(self.t))
        self.assertEqual(self.store.get("account"), before)
        self.assertEqual(self.paper.account, before)
        self.assertEqual(self.store.order("buy")["status"], "PENDING")

    def test_spread_limit(self):
        self.paper.submit("buy", "BUY", "0.5", point(self.t))
        self.advance("100", "101")
        self.assertEqual(self.paper.step(self.market, point(self.t))["status"], "REJECTED")

    def test_new_day_exit_loss_is_not_reset_by_next_buy(self):
        self.buy()
        self.t = (self.t // 86_400_000 + 1) * 86_400_000
        self.advance("50", "50.01")
        self.paper.submit("exit", "SELL", "0.5", point(self.t), True)
        self.advance("50", "50.01")
        self.assertEqual(self.paper.step(self.market, point(self.t))["status"], "FILLED")
        self.store.close()
        self.store = Store(str(Path(self.tmp.name)/"test.sqlite"))
        self.paper = Paper(self.config, self.store)
        self.paper.submit("reenter", "BUY", "0.1", point(self.t))
        self.advance("50", "50.01")
        self.assertEqual(self.paper.step(self.market, point(self.t))["status"], "REJECTED")

    def test_reduce_only_can_exit_with_fresh_book_without_trade_stream(self):
        self.buy()
        self.paper.submit("exit", "SELL", "0.5", point(self.t), True)
        self.advance()
        self.market.disconnect("market")
        self.assertEqual(self.paper.step(self.market, point(self.t))["status"], "FILLED")
        self.assertEqual(D(self.paper.account["quantity"]), 0)

    def test_reduce_only_still_rejects_stale_book(self):
        self.buy()
        self.paper.submit("exit", "SELL", "0.5", point(self.t), True)
        self.assertEqual(self.paper.step(self.market, point(self.t + 3000))["status"], "REJECTED")
        self.assertEqual(D(self.paper.account["quantity"]), D("0.5"))

    def test_raw_event_journal(self):
        row = self.store.db.execute("SELECT * FROM events ORDER BY id LIMIT 1").fetchone()
        self.assertEqual(row["received_ms"], self.t)
        self.assertEqual(json.loads(row["payload"])["e"], "aggTrade")


if __name__ == "__main__":
    unittest.main()
