import asyncio
import json
from unittest.mock import AsyncMock, Mock
import unittest
from unittest.mock import patch

from apex_flow.collector import collect_group, run_collector, stream_urls
from apex_flow.clock import SystemClock
from apex_flow.observability.metrics import Metrics
from apex_flow.config import Config
from apex_flow.market import Market


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    def test_endpoint_routing(self):
        urls = stream_urls("BTCUSDT")
        self.assertIn("/public/stream?streams=btcusdt@depth20@100ms", urls["public"])
        self.assertIn("/market/stream?streams=btcusdt@aggTrade/btcusdt@markPrice@1s", urls["market"])

    async def test_watchdog_runs_without_messages_and_workers_stop(self):
        stopped, ticks = [], []

        async def worker(group, *args):
            try:
                await asyncio.Future()
            finally:
                stopped.append(group)

        clock = SystemClock()
        with patch("apex_flow.collector.collect_group", worker):
            await run_collector(Config(), Market("BTCUSDT"), None, ticks.append, 0.02, clock, Metrics(clock), "test")
        self.assertTrue(ticks)
        self.assertEqual(set(stopped), {"public", "market"})


class ProcessingFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_oserror_is_fatal_not_network_retry(self):
        class Socket:
            latency = 0
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                return json.dumps({"stream": "test", "data": {}})

        market, store = Mock(), Mock()
        market.connected = set()
        clock = SystemClock()
        callback = Mock(side_effect=OSError("internal disk failure"))
        with patch("apex_flow.collector.connect", return_value=Socket()) as connect, \
                patch("apex_flow.collector.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(RuntimeError) as raised:
                await collect_group("public", "unused", market, store, callback, clock, Metrics(clock), "test")
        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(callback.call_count, 1)
