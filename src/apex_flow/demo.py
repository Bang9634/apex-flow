"""오프라인 예제. 합성 이벤트와 시계는 실제 운영 모듈에서 사용하지 않는다."""
import json
import time
import uuid
from decimal import Decimal as D

from .clock import TimePoint
from .market import Market
from .paper import Paper


def point(ms):
    """합성/replay 전용 결정적 시각. 서로 다른 실행의 monotonic 값을 섞지 않는다."""
    return TimePoint(ms, ms * 1_000_000)


def synthetic(market, store, t, update, bid="100", ask="100.02"):
    """오프라인 demo/테스트용 이벤트를 만든다. 실제 BTC 가격이나 실제 시계 흐름이 아니다."""
    events = [
        (f"{market.symbol.lower()}@aggTrade", dict(e="aggTrade", E=t, s=market.symbol, a=update, p=bid, q="1", m=False)),
        (f"{market.symbol.lower()}@markPrice@1s", dict(e="markPriceUpdate", E=t, s=market.symbol, p=bid)),
        (f"{market.symbol.lower()}@depth20@100ms", dict(e="depthUpdate", E=t, s=market.symbol, u=update, b=[[bid,"2"]], a=[[ask,"2"]])),
    ]
    for stream, event in events:
        store.event(stream, event, t)
        market.apply(stream, event, point(t))


def demo(config, store):
    """합성 가격에서 매수/매도·비용·회계를 확인한다. 기존 DB 잔고는 이어서 사용한다."""
    paper, market = Paper(config, store), Market(config.symbol)
    if D(paper.account["quantity"]):
        raise ValueError("Demo requires a flat account; use a dedicated database")
    market.connected = {"public", "market"}
    t = time.time_ns() // 1_000_000
    synthetic(market, store, t, 1)
    paper.submit(uuid.uuid4().hex, "BUY", "0.5", point(t))
    t += config.latency_ms + 1
    synthetic(market, store, t, 2)
    print(json.dumps(paper.step(market, point(t))))
    paper.submit(uuid.uuid4().hex, "SELL", "0.5", point(t), reduce_only=True)
    t += config.latency_ms + 1
    synthetic(market, store, t, 3, "100.10", "100.12")
    print(json.dumps(paper.step(market, point(t))))
    print(json.dumps({"account": paper.account, "features": market.features()}))