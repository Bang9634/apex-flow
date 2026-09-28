from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import re
import tomllib


@dataclass(frozen=True)
class Config:
    """단일 종목 paper 설정. 금액은 Decimal, 비용은 편도 bps, 시간은 ms다."""
    symbol: str = "BTCUSDT"
    database: str = "data/paper.sqlite3"
    initial_cash: Decimal = Decimal("10000")
    max_notional: Decimal = Decimal("100")
    max_daily_loss: Decimal = Decimal("20")
    max_spread_bps: Decimal = Decimal("10")
    taker_fee_bps: Decimal = Decimal("5")
    slippage_bps: Decimal = Decimal("2")
    latency_ms: int = 100
    stale_ms: int = 2000
    kill_file: str = "data/KILL"

    def __post_init__(self):
        """생성 시 위험한 값과 비정상 숫자를 거절한다. 거래소 종목 조회는 하지 않는다."""
        if not re.fullmatch(r"[A-Z0-9]{5,24}", self.symbol):
            raise ValueError("Invalid symbol")
        for name in ("initial_cash", "max_notional", "max_daily_loss", "max_spread_bps",
                     "taker_fee_bps", "slippage_bps"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"Invalid {name}")
        if self.initial_cash <= 0 or not 0 < self.max_notional <= self.initial_cash:
            raise ValueError("Require 0 < max_notional <= initial_cash (1x maximum)")
        if self.max_daily_loss <= 0 or self.max_spread_bps <= 0:
            raise ValueError("Risk limits must be positive")
        if self.slippage_bps >= 10000 or self.taker_fee_bps >= 10000:
            raise ValueError("Invalid cost bps")
        if type(self.latency_ms) is not int or type(self.stale_ms) is not int:
            raise ValueError("Timing settings must be integers")
        if self.latency_ms < 0 or self.stale_ms <= 0:
            raise ValueError("Invalid timing")

    @classmethod
    def load(cls, path: str):
        """TOML을 읽고 금액을 Decimal로 변환한다. 상대 경로는 실행 디렉터리 기준이다."""
        with open(path, "rb") as file:
            values = tomllib.load(file)
        for name in ("initial_cash", "max_notional", "max_daily_loss", "max_spread_bps",
                     "taker_fee_bps", "slippage_bps"):
            if name in values:
                values[name] = Decimal(str(values[name]))
        return cls(**values)

    def killed(self):
        """파일 존재 여부만 반환한다. 신규 매수 차단이며 자동 청산 명령이 아니다."""
        return Path(self.kill_file).exists()
