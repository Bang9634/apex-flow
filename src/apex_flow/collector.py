"""공개 시세 연결과 재접속을 담당한다. 계좌 인증·주문 전송 기능은 없다."""
import asyncio
import json
import logging
import random
import time

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

log = logging.getLogger(__name__)


def now_ms():
    """로컬 wall-clock ms. 거래소 시각 비교용이며 monotonic 경과 시간과 다르다."""
    return time.time_ns() // 1_000_000


def stream_urls(symbol):
    """호가(public)와 체결/mark(market)를 서로 다른 연결로 구독한다."""
    symbol = symbol.lower()
    base = "wss://fstream.binance.com"
    return {
        "public": f"{base}/public/stream?streams={symbol}@depth20@100ms",
        "market": f"{base}/market/stream?streams={symbol}@aggTrade/{symbol}@markPrice@1s",
    }


async def collect_group(group, url, market, store, on_event):
    """한 연결의 수신 루프. 원본 저장 → 상태 반영 → 주문/상태 콜백 순으로 처리한다."""
    failures = 0
    while True:
        try:
            async with connect(url, open_timeout=10, close_timeout=5, ping_interval=20,
                               ping_timeout=20, max_queue=32, max_size=1_048_576) as socket:
                market.connected.add(group)
                log.info("connected group=%s", group)
                async for raw in socket:
                    received = now_ms()
                    try:
                        envelope = json.loads(raw)
                        stream, payload = envelope["stream"], envelope["data"]
                        # Internal errors must escape the network retry handler below.
                        store.event(stream, payload, received)
                        market.apply(stream, payload, received)
                        on_event(received)
                    except Exception as exc:
                        raise RuntimeError(f"Market event processing failed: {group}") from exc
                    failures = 0
        except (OSError, TimeoutError, ConnectionClosed, InvalidStatus) as exc:
            failures += 1
            log.warning("connection_failed group=%s count=%s error=%s", group, failures, type(exc).__name__)
            if failures >= 8:
                raise RuntimeError(f"Circuit breaker: {group} connection failed 8 times") from exc
        finally:
            market.disconnect(group)
        # 지수 backoff와 작은 무작위 지연으로 재접속 요청의 집중을 줄인다.
        await asyncio.sleep(min(30, 2 ** min(failures, 5)) + random.random())


async def run_collector(config, market, store, on_event, duration):
    """두 수신 작업과 heartbeat를 운영한다. 작업 실패 시 TaskGroup이 나머지도 취소한다."""
    async with asyncio.TaskGroup() as tasks:
        workers = [tasks.create_task(collect_group(group, url, market, store, on_event))
                   for group, url in stream_urls(config.symbol).items()]
        deadline = asyncio.get_running_loop().time() + duration
        # 데이터가 조용해도 대기 주문 검사와 상태 출력이 멈추지 않게 한다.
        # 콜백은 동기 호출이므로 내부 DB 쓰기는 여전히 이벤트 루프를 지연시킬 수 있다.
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(min(0.25, max(0, deadline - asyncio.get_running_loop().time())))
            on_event(now_ms())
        for worker in workers:
            worker.cancel()
