import asyncio
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from apex_flow.clock import TimePoint
from apex_flow.collector import collect_group
from apex_flow.config import Config
from apex_flow.market import Market
from apex_flow.observability.metrics import Metrics
from apex_flow.observability.reporting import JsonlReporter
from apex_flow.paper import Paper
from apex_flow.storage import Store


class FakeClock:
    def __init__(self):
        self.value = TimePoint(1_800_000_000_000, 0)

    def now(self):
        return self.value

    def advance(self, ms, wall_ms=None):
        self.value = TimePoint(self.value.wall_ms + (ms if wall_ms is None else wall_ms),
                               self.value.mono_ns + ms * 1_000_000)


class MetricTests(unittest.TestCase):
    def test_bounded_window_percentiles_and_lifetime_totals(self):
        metrics = Metrics(FakeClock(), capacity=3)
        for value in (1, 2, 3, 100):
            metrics.observe("latency_ms", value)
        summary = metrics.snapshot()["measurements"]["latency_ms"]
        self.assertEqual(summary["count"], 4)
        self.assertEqual(summary["window_count"], 3)
        self.assertEqual(summary["window_p50"], 3)
        self.assertEqual(summary["window_p99"], 100)
        self.assertEqual(summary["mean"], 26.5)
        self.assertEqual(summary["min"], 1)

    def test_measure_uses_monotonic_despite_wall_jump(self):
        clock = FakeClock()
        metrics = Metrics(clock)
        with metrics.measure("work_ms"):
            clock.advance(20, wall_ms=-60000)
        metrics.check_clock(clock.now())
        self.assertEqual(metrics.snapshot()["measurements"]["work_ms"]["mean"], 20)
        self.assertEqual(metrics.counters["clock.jump_count"], 1)

    def test_negative_arrival_age_is_not_clamped(self):
        metrics = Metrics(FakeClock())
        metrics.observe("arrival_age_ms", -50)
        self.assertEqual(metrics.snapshot()["measurements"]["arrival_age_ms"]["min"], -50)
        # 잘못된 표본을 거절한 뒤에도 final 스냅샷을 만들 수 있어야 한다.
        with self.assertRaises(ValueError):
            metrics.observe("invalid", float("nan"))
        self.assertNotIn("invalid", metrics.snapshot()["measurements"])

    def test_outage_is_counted_even_without_messages(self):
        clock = FakeClock()
        metrics = Metrics(clock)
        metrics.connection("public", False, clock.now())
        clock.advance(1000)
        self.assertEqual(metrics.snapshot()["connections"]["public"]["down_ms"], 1000)
        metrics.connection("public", True, clock.now())
        clock.advance(500)
        metrics.connection("public", False, clock.now())
        clock.advance(250)
        self.assertEqual(metrics.snapshot()["connections"]["public"]["down_ms"], 1250)

    def test_freshness_expires_after_wall_clock_moves_backwards(self):
        market = Market("BTCUSDT")
        market.connected = {"public", "market"}
        received = TimePoint(10000, 0)
        market.bids, market.asks = [(Decimal(100), Decimal(1))], [(Decimal(101), Decimal(1))]
        for kind in ("depthUpdate", "aggTrade", "markPriceUpdate"):
            market.last[kind] = (received, 10000)
        self.assertTrue(market.healthy(TimePoint(5000, 100_000_000), 2000))
        self.assertFalse(market.healthy(TimePoint(10000, 3_000_000_000), 2000))

    def test_slow_network_is_not_made_fresh_by_recent_receipt(self):
        market = Market("BTCUSDT")
        market.last["aggTrade"] = (TimePoint(10000, 0), 7000)
        self.assertFalse(market.fresh("aggTrade", TimePoint(10000, 0), 2000))

    def test_existing_database_migration_preserves_events_and_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/"old.sqlite3")
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE events(id INTEGER PRIMARY KEY, received_ms INTEGER NOT NULL,
                                    stream TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO events VALUES(1,123,'old','{}');
                INSERT INTO state VALUES('account','{"cash":"42"}');
            """)
            db.close()
            store = Store(path)
            try:
                self.assertEqual(store.get("account"), {"cash": "42"})
                self.assertIsNone(store.db.execute("SELECT received_mono_ns FROM events WHERE id=1").fetchone()[0])
                store.event("new", {}, 456, received_mono_ns=100, run_id="run-test")
                row = store.db.execute("SELECT * FROM events WHERE id=2").fetchone()
                self.assertEqual((row["received_mono_ns"], row["run_id"]), (100, "run-test"))
            finally:
                store.close()

    def test_wall_jump_does_not_fill_order_before_monotonic_latency(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory)/"paper.sqlite3"))
            try:
                config = Config(kill_file=str(Path(directory)/"KILL"))
                paper = Paper(config, store)
                market = Market(config.symbol)
                market.connected = {"public", "market"}
                start = TimePoint(1_800_000_000_000, 0)
                paper.submit("buy", "BUY", "0.5", start)
                jump = TimePoint(start.wall_ms + 60000, 50_000_000)
                self.assertIsNone(paper.step(market, jump))
                final = TimePoint(start.wall_ms + 60051, 101_000_000)
                for stream, event in (
                    ("btcusdt@depth20@100ms", dict(e="depthUpdate", u=1, b=[["100","1"]], a=[["100.01","1"]])),
                    ("btcusdt@aggTrade", dict(e="aggTrade", a=1,p="100",q="1",m=False)),
                    ("btcusdt@markPrice@1s", dict(e="markPriceUpdate",p="100"))):
                    market.apply(stream, dict(event, E=final.wall_ms, s=config.symbol), final)
                self.assertEqual(paper.step(market, final)["status"], "FILLED")
                self.assertEqual(json.loads(store.order("buy")["result"])["simulated_wait_ms"], 101)
            finally:
                store.close()


class AsyncMetricTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_does_not_overwrite_and_writes_parseable_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"metrics.jsonl"
            reporter = JsonlReporter(path, {"run_id": "test"})
            await reporter.write({"type": "final", "outcome": "completed"})
            rows = [json.loads(row) for row in path.read_text().splitlines()]
            self.assertEqual([row["type"] for row in rows], ["run", "final"])
            with self.assertRaises(FileExistsError):
                JsonlReporter(path, {})
            self.assertEqual(len(path.read_text().splitlines()), 2)

    async def test_network_failure_report_has_no_fake_zero_latency(self):
        clock = FakeClock()
        metrics = Metrics(clock)
        with patch("apex_flow.collector.connect", side_effect=OSError("offline")), \
                patch("apex_flow.collector.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(RuntimeError):
                await collect_group("public", "unused", Market("BTCUSDT"), Mock(), Mock(), clock, metrics, "run")
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["counters"]["public.network_errors"], 8)
        self.assertFalse(snapshot["connections"]["public"]["connected"])
        self.assertNotIn("public.ws_rtt_ms", snapshot["measurements"])

    async def test_callback_receives_time_after_slow_storage(self):
        clock = FakeClock()
        metrics = Metrics(clock)
        received_by_callback = []

        class Socket:
            latency = .05
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def __aiter__(self): return self
            async def __anext__(self):
                return json.dumps({"stream": "test", "data": {}})

        def callback(now):
            received_by_callback.append(now)
            raise ValueError("end test")

        store = Mock()
        store.event.side_effect = lambda *args, **kwargs: clock.advance(3000)
        market = Mock()
        market.connected = set()
        with patch("apex_flow.collector.connect", return_value=Socket()):
            with self.assertRaises(RuntimeError):
                await collect_group("public", "unused", market, store, callback, clock, metrics, "test")
        self.assertEqual(received_by_callback[0].mono_ns, 3_000_000_000)
        self.assertEqual(metrics.snapshot()["measurements"]["processing.persist_ms"]["mean"], 3000)
        self.assertEqual(metrics.snapshot()["measurements"]["public.ws_rtt_ms"]["mean"], 50)


class RuntimeReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_still_writes_final_report(self):
        from apex_flow.runtime import SessionOptions, run_market_session
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"metrics.jsonl"
            store = Store(str(Path(directory)/"paper.sqlite3"))
            try:
                options = SessionOptions("collect", metrics_output=str(path))
                with patch("apex_flow.runtime.run_collector", new_callable=AsyncMock) as collector:
                    collector.side_effect = RuntimeError("injected collector error")
                    with self.assertRaises(ExceptionGroup):
                        await run_market_session(Config(), store, options)
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertEqual(rows[-1]["outcome"], "error")
                self.assertEqual(rows[-1]["measurements"], {})
                self.assertEqual(rows[0]["database"], str(store.path))
            finally:
                store.close()

    async def test_cancellation_writes_final_and_stops_report_task(self):
        from apex_flow.runtime import SessionOptions, run_market_session
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"metrics.jsonl"
            store = Store(str(Path(directory)/"paper.sqlite3"))
            started = asyncio.Event()
            async def collector(*args):
                started.set()
                await asyncio.Future()
            try:
                options = SessionOptions("collect", metrics_output=str(path))
                with patch("apex_flow.runtime.run_collector", collector):
                    task = asyncio.create_task(run_market_session(Config(), store, options))
                    await started.wait()
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertEqual(rows[-1]["outcome"], "cancelled")
                self.assertEqual([row["type"] for row in rows], ["run", "final"])
            finally:
                store.close()
