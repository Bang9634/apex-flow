"""UTC 표시는 wall clock, 같은 실행 안의 경과 시간은 monotonic clock으로 계산한다."""
from dataclasses import dataclass
from typing import Protocol
import time


@dataclass(frozen=True)
class TimePoint:
    wall_ms: int
    mono_ns: int

    def elapsed_ms(self, earlier: "TimePoint") -> float:
        return (self.mono_ns - earlier.mono_ns) / 1_000_000


class Clock(Protocol):
    def now(self) -> TimePoint: ...


class SystemClock:
    def now(self) -> TimePoint:
        return TimePoint(time.time_ns() // 1_000_000, time.monotonic_ns())
