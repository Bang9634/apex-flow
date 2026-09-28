"""Top-20 snapshots, not a reconstructed full diff-depth order book."""
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
import math
import statistics

D = Decimal


def positive(value):
    """가격·수량에 NaN, 무한대, 0, 음수가 들어오는 것을 막는다."""
    result = D(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError("Expected finite positive market value")
    return result


@dataclass
class Market:
    """메모리의 시장 상태. last는 이벤트별 (로컬 수신 ms, 거래소 이벤트 ms)다."""
    symbol: str
    bids: list = field(default_factory=list)
    asks: list = field(default_factory=list)
    last: dict = field(default_factory=dict)
    connected: set = field(default_factory=set)
    update_id: int = -1
    # 최근 100개 집계 체결 표본이다. 최근 100초라는 의미가 아니다.
    prices: deque = field(default_factory=lambda: deque(maxlen=100))
    flows: deque = field(default_factory=lambda: deque(maxlen=100))
    trade_id: int = -1
    mark: Decimal | None = None

    def disconnect(self, group):
        """단절된 연결의 유효성을 제거한다. 과거 호가를 새 호가처럼 쓰지 않는다."""
        self.connected.discard(group)
        if group == "public":
            self.bids, self.asks = [], []
            self.last.pop("depthUpdate", None)
        else:
            self.last.pop("aggTrade", None)
            self.last.pop("markPriceUpdate", None)
            self.prices.clear()
            self.flows.clear()

    def apply(self, stream, event, now_ms):
        """이벤트를 검증·적용하면 True, 대상 밖/중복이면 False, 잘못된 값이면 예외다."""
        if event.get("s") != self.symbol or event.get("st", 1) != 1:
            return False
        kind = event.get("e")
        timestamp = int(event["E"])
        if timestamp > now_ms + 1000 or timestamp < 0:
            raise ValueError("Invalid exchange event time / clock skew")
        if kind in self.last and timestamp < self.last[kind][1]:
            return False
        if kind == "depthUpdate":
            if stream != f"{self.symbol.lower()}@depth20@100ms":
                raise ValueError("Only partial depth snapshots are accepted")
            update = int(event["u"])
            if update <= self.update_id:
                return False
            bids = sorted([(positive(p), positive(q)) for p, q in event["b"] if D(q) != 0], reverse=True)
            asks = sorted([(positive(p), positive(q)) for p, q in event["a"] if D(q) != 0])
            if not bids or not asks or bids[0][0] >= asks[0][0]:
                raise ValueError("Empty or crossed book")
            if len({p for p, _ in bids}) != len(bids) or len({p for p, _ in asks}) != len(asks):
                raise ValueError("Duplicate depth price")
            # partial depth는 상위 호가 전체를 교체한다. diff-depth 누적 복원과 다르다.
            self.bids, self.asks, self.update_id = bids, asks, update
        elif kind == "aggTrade":
            trade_id = int(event["a"])
            if trade_id <= self.trade_id:
                return False
            price, quantity = positive(event["p"]), positive(event["q"])
            if type(event["m"]) is not bool:
                raise ValueError("Invalid aggressor flag")
            self.prices.append(float(price))
            # m=True는 매수자가 maker: 적극적으로 체결한 쪽은 매도이므로 음수다.
            self.flows.append(-quantity if event["m"] else quantity)
            self.trade_id = trade_id
        elif kind == "markPriceUpdate":
            self.mark = positive(event["p"])
        else:
            return False
        self.last[kind] = (now_ms, timestamp)
        return True

    def book_healthy(self, now_ms, stale_ms):
        """Executable quotes only; exits must not depend on unrelated trade/mark feeds."""
        return ("public" in self.connected and bool(self.bids and self.asks)
                and "depthUpdate" in self.last
                and all(-1000 <= now_ms - t <= stale_ms for t in self.last["depthUpdate"]))

    def healthy(self, now_ms, stale_ms):
        """신규 진입 조건. 두 연결과 세 이벤트가 모두 최신이어야 한다.

        수신 시각만 검사하면 지연 도착한 과거 데이터를 최신으로 오인한다.
        현재는 wall clock 기반이며 미래 시각에 1초 여유를 둔다.
        """
        return (self.connected == {"public", "market"} and bool(self.bids and self.asks)
                and all(kind in self.last and all(-1000 <= now_ms - t <= stale_ms for t in self.last[kind])
                        for kind in ("depthUpdate", "aggTrade", "markPriceUpdate")))

    def features(self):
        """기본 수치 지표를 반환한다. 준비되지 않은 지표는 None이며 매매 신호는 아니다."""
        if not self.bids or not self.asks:
            return {}
        bid, bq = self.bids[0]
        ask, aq = self.asks[0]
        mid = (bid + ask) / 2
        bv, av = sum(q for _, q in self.bids), sum(q for _, q in self.asks)
        # momentum은 표본 첫/끝 가격 수익률, volatility는 로그수익률의 표준편차다.
        # 연율화하지 않으며 거래 빈도가 변하면 표본의 시간 길이도 달라진다.
        returns = [math.log(b/a) for a, b in zip(self.prices, list(self.prices)[1:])]
        flow_total = sum(abs(q) for q in self.flows)
        return dict(mid=str(mid), spread_bps=str((ask-bid)/mid*10000),
                    imbalance=str((bv-av)/(bv+av)), microprice=str((ask*bq+bid*aq)/(bq+aq)),
                    momentum=self.prices[-1]/self.prices[0]-1 if len(self.prices) > 1 else None,
                    volatility=statistics.pstdev(returns) if len(returns) > 1 else None,
                    trade_flow=str(sum(self.flows)/flow_total) if flow_total else None)
