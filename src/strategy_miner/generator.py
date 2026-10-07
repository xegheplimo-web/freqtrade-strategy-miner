from __future__ import annotations

import random

from .genome import StrategyGenome


class GenomeGenerator:
    def __init__(self, seed: int, timeframe: str = "5m", can_short: bool = True) -> None:
        self.rng = random.Random(seed)
        self.timeframe = timeframe
        self.can_short = can_short

    def generate_one(self, strategy_id: int) -> StrategyGenome:
        ema_fast = self.rng.randint(5, 50)
        ema_slow = self.rng.randint(max(ema_fast + 10, 55), 250)

        genome = StrategyGenome(
            strategy_id=strategy_id,
            timeframe=self.timeframe,
            can_short=self.can_short,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            rsi_period=self.rng.randint(7, 30),
            rsi_long_max=self.rng.randint(22, 45),
            rsi_short_min=self.rng.randint(55, 78),
            stoploss=round(-self.rng.uniform(0.03, 0.25), 3),
        )
        genome.validate()
        return genome

    def generate_many(self, count: int, start_id: int = 1) -> list[StrategyGenome]:
        return [self.generate_one(i) for i in range(start_id, start_id + count)]
