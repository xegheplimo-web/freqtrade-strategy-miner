from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class StrategyGenome:
    strategy_id: int
    timeframe: str
    can_short: bool
    ema_fast: int
    ema_slow: int
    rsi_period: int
    rsi_long_max: int
    rsi_short_min: int
    stoploss: float

    @property
    def class_name(self) -> str:
        return f"Miner_{self.strategy_id:06d}"

    def validate(self) -> None:
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be less than ema_slow")
        if not 2 <= self.rsi_period <= 100:
            raise ValueError("rsi_period out of range")
        if not 1 <= self.rsi_long_max < 50:
            raise ValueError("rsi_long_max must be in [1, 49]")
        if not 50 < self.rsi_short_min <= 99:
            raise ValueError("rsi_short_min must be in [51, 99]")
        if not -0.99 < self.stoploss < 0:
            raise ValueError("stoploss must be negative and greater than -0.99")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
