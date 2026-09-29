"""단일 이벤트 루프에서 갱신하는 bounded 집계. 원본 이벤트별 파일 쓰기는 하지 않는다."""
from collections import Counter, deque
from contextlib import contextmanager
import math

from ..clock import Clock, TimePoint


class Distribution:
    def __init__(self, capacity):
        self.samples = deque(maxlen=capacity)
        self.count = 0
        self.total = 0.0
        self.minimum = math.inf
        self.maximum = -math.inf

    def add(self, value):
        if not math.isfinite(value):
            raise ValueError("Metric must be finite")
        self.samples.append(value)
        self.count += 1
        self.total += value
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)

    def summary(self):
        ordered = sorted(self.samples)
        def percentile(p):
            return ordered[max(0, math.ceil(len(ordered) * p) - 1)]
        return dict(count=self.count, mean=self.total/self.count,
                    min=self.minimum, max=self.maximum, window_count=len(ordered),
                    window_p50=percentile(.50), window_p95=percentile(.95), window_p99=percentile(.99))


class Metrics:
    """누적 count/mean/min/max와 최근 N개 표본의 백분위수를 구분한다."""
    def __init__(self, clock: Clock, capacity=4096):
        if capacity <= 0:
            raise ValueError("Metric capacity must be positive")
        self.clock = clock
        self.capacity = capacity
        self.started = clock.now()
        self.counters = Counter()
        self.distributions = {}
        self.connections = {}
        self.last_clock = self.started

    def increment(self, name, amount=1):
        self.counters[name] += amount

    def observe(self, name, value):
        # 이름은 코드에서 정한 유한한 집합만 사용한다. 주문 ID를 label로 넣지 않는다.
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("Metric must be finite")
        if name not in self.distributions:
            self.distributions[name] = Distribution(self.capacity)
        self.distributions[name].add(value)

    @contextmanager
    def measure(self, name):
        start = self.clock.now()
        try:
            yield
        finally:
            self.observe(name, self.clock.now().elapsed_ms(start))

    def check_clock(self, now: TimePoint):
        drift = now.wall_ms - self.last_clock.wall_ms - now.elapsed_ms(self.last_clock)
        if abs(drift) > 250:
            self.increment("clock.jump_count")
            self.observe("clock.jump_ms", drift)
        self.last_clock = now

    def connection(self, group, connected, now):
        previous = self.connections.get(group)
        if previous is None:
            self.connections[group] = dict(connected=connected, since=now, down_ms=0.0)
        elif previous["connected"] != connected:
            if not previous["connected"]:
                previous["down_ms"] += now.elapsed_ms(previous["since"])
            previous.update(connected=connected, since=now)

    def snapshot(self):
        now = self.clock.now()
        connections = {}
        for group, state in self.connections.items():
            down = state["down_ms"] + (0 if state["connected"] else now.elapsed_ms(state["since"]))
            connections[group] = dict(connected=state["connected"], down_ms=down)
        return dict(wall_ms=now.wall_ms, elapsed_ms=now.elapsed_ms(self.started),
                    percentile_scope=f"last {self.capacity} observations per metric",
                    counters=dict(self.counters), connections=connections,
                    measurements={name: value.summary() for name, value in sorted(self.distributions.items())})
