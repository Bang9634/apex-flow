"""Binance 공개 수신 어댑터. 실행 정책과 보고서 저장은 runtime에서 조립한다."""
import asyncio
import json
import logging
import random

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .clock import Clock
from .observability.metrics import Metrics

log = logging.getLogger(__name__)
EVENT_KINDS = {"depthUpdate", "aggTrade", "markPriceUpdate"}


def stream_urls(symbol):
    symbol = symbol.lower()
    base = "wss://fstream.binance.com"
    return {
        "public": f"{base}/public/stream?streams={symbol}@depth20@100ms",
        "market": f"{base}/market/stream?streams={symbol}@aggTrade/{symbol}@markPrice@1s",
    }


async def collect_group(group, url, market, store, on_event, clock: Clock, metrics: Metrics, run_id):
    """수신/파싱/저장/시장 반영/콜백 시간을 분리한다. 네트워크 오류만 재시도한다."""
    failures = 0
    metrics.connection(group, False, clock.now())
    while True:
        started = clock.now()
        metrics.increment(f"{group}.connection_attempts")
        try:
            async with connect(url, open_timeout=10, close_timeout=5, ping_interval=20,
                               ping_timeout=20, max_queue=32, max_size=1_048_576) as socket:
                metrics.observe(f"{group}.connect_ms", clock.now().elapsed_ms(started))
                metrics.connection(group, True, clock.now())
                metrics.increment(f"{group}.connections")
                market.connected.add(group)
                last_rtt = 0.0
                last_received = None
                log.info("connected group=%s", group)
                async for raw in socket:
                    received = clock.now()
                    metrics.check_clock(received)
                    metrics.increment(f"{group}.messages")
                    metrics.increment(f"{group}.payload_bytes", len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw))
                    if last_received:
                        metrics.observe(f"{group}.interarrival_ms", received.elapsed_ms(last_received))
                    last_received = received
                    # 라이브러리 자동 ping의 최신 RTT. 미측정 상태인 0을 관측값으로 기록하지 않는다.
                    if socket.latency > 0 and socket.latency != last_rtt:
                        metrics.observe(f"{group}.ws_rtt_ms", socket.latency * 1000)
                        last_rtt = socket.latency
                    try:
                        with metrics.measure("processing.parse_ms"):
                            envelope = json.loads(raw)
                            stream, payload = envelope["stream"], envelope["data"]
                            kind = payload.get("e")
                            if kind in EVENT_KINDS:
                                age = received.wall_ms - int(payload["E"])
                                metrics.observe(f"{kind}.arrival_age_ms", age)
                                if age < 0:
                                    metrics.increment(f"{kind}.negative_age")
                        with metrics.measure("processing.persist_ms"):
                            store.event(stream, payload, received.wall_ms,
                                        received_mono_ns=received.mono_ns, run_id=run_id)
                        with metrics.measure("processing.market_ms"):
                            accepted = market.apply(stream, payload, received)
                        metrics.increment("events.accepted" if accepted else "events.ignored")
                        # DB 쓰기에 시간이 걸렸다면 과거 수신 시각으로 freshness를 검사하면 안 된다.
                        with metrics.measure("processing.callback_ms"):
                            on_event(clock.now())
                    except Exception as exc:
                        metrics.increment("processing.errors")
                        raise RuntimeError(f"Market event processing failed: {group}") from exc
                    finally:
                        metrics.observe("processing.total_ms", clock.now().elapsed_ms(received))
                    failures = 0
                metrics.increment(f"{group}.remote_closes")
        except (OSError, TimeoutError, ConnectionClosed, InvalidStatus) as exc:
            failures += 1
            metrics.increment(f"{group}.network_errors")
            log.warning("connection_failed group=%s count=%s error=%s", group, failures, type(exc).__name__)
            if failures >= 8:
                raise RuntimeError(f"Circuit breaker: {group} connection failed 8 times") from exc
        finally:
            market.disconnect(group)
            metrics.connection(group, False, clock.now())
        metrics.increment(f"{group}.retries")
        await asyncio.sleep(min(30, 2 ** min(failures, 5)) + random.random())


async def run_collector(config, market, store, on_event, duration, clock, metrics, run_id):
    """monotonic deadline과 heartbeat. 연결 작업 실패 시 다른 작업도 취소한다."""
    for group in stream_urls(config.symbol):
        metrics.connection(group, False, clock.now())
    async with asyncio.TaskGroup() as tasks:
        workers = [tasks.create_task(collect_group(group, url, market, store, on_event, clock, metrics, run_id))
                   for group, url in stream_urls(config.symbol).items()]
        start = clock.now()
        previous_tick = start
        while clock.now().elapsed_ms(start) < duration * 1000:
            remaining = duration * 1000 - clock.now().elapsed_ms(start)
            delay_ms = min(250, max(0, remaining))
            scheduled = clock.now()
            await asyncio.sleep(delay_ms / 1000)
            now = clock.now()
            metrics.check_clock(now)
            metrics.observe("loop.lag_ms", max(0, now.elapsed_ms(scheduled) - delay_ms))
            metrics.increment("health.samples")
            if not market.healthy(now, config.stale_ms):
                metrics.increment("health.unhealthy_samples")
                metrics.increment("health.unhealthy_ms_estimate", now.elapsed_ms(previous_tick))
            for kind in EVENT_KINDS:
                if kind in market.last:
                    metrics.observe(f"{kind}.receipt_silence_ms", now.elapsed_ms(market.last[kind][0]))
            with metrics.measure("processing.heartbeat_callback_ms"):
                on_event(now)
            previous_tick = now
        for worker in workers:
            worker.cancel()
