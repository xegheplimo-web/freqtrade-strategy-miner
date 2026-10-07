from __future__ import annotations

import ast
from pathlib import Path

from .genome import StrategyGenome

STRATEGY_TEMPLATE = '''from freqtrade.strategy import IStrategy, IntParameter
from pandas import DataFrame
import talib.abstract as ta


class {class_name}(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = {timeframe!r}
    can_short = {can_short}
    process_only_new_candles = True
    startup_candle_count = {startup_candle_count}

    minimal_roi = {{"0": 0.10, "60": 0.04, "180": 0.0}}
    stoploss = {stoploss}
    trailing_stop = False
    use_exit_signal = True

    buy_rsi_long_max = IntParameter(1, 49, default={rsi_long_max}, space="buy")
    buy_rsi_short_min = IntParameter(51, 99, default={rsi_short_min}, space="buy")

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod={ema_fast})
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod={ema_slow})
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod={rsi_period})
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        long_condition = (
            (dataframe["ema_fast"] > dataframe["ema_slow"]) &
            (dataframe["rsi"] < self.buy_rsi_long_max.value) &
            (dataframe["volume"] > 0)
        )
        dataframe.loc[long_condition, ["enter_long", "enter_tag"]] = (1, "trend_rsi_long")

        if self.can_short:
            short_condition = (
                (dataframe["ema_fast"] < dataframe["ema_slow"]) &
                (dataframe["rsi"] > self.buy_rsi_short_min.value) &
                (dataframe["volume"] > 0)
            )
            dataframe.loc[short_condition, ["enter_short", "enter_tag"]] = (1, "trend_rsi_short")

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        long_exit = (
            (dataframe["ema_fast"] < dataframe["ema_slow"]) &
            (dataframe["volume"] > 0)
        )
        dataframe.loc[long_exit, ["exit_long", "exit_tag"]] = (1, "trend_flip_long")

        if self.can_short:
            short_exit = (
                (dataframe["ema_fast"] > dataframe["ema_slow"]) &
                (dataframe["volume"] > 0)
            )
            dataframe.loc[short_exit, ["exit_short", "exit_tag"]] = (1, "trend_flip_short")

        return dataframe
'''


def compile_strategy(genome: StrategyGenome) -> str:
    """Generated strategies expose buy-space IntParameters (entry RSI thresholds);
    roi/stoploss spaces use freqtrade defaults; sell space intentionally unused
    (exits are signal flips)."""
    genome.validate()
    source = STRATEGY_TEMPLATE.format(
        class_name=genome.class_name,
        timeframe=genome.timeframe,
        can_short=repr(genome.can_short),
        startup_candle_count=max(genome.ema_slow + 20, 300),
        stoploss=genome.stoploss,
        ema_fast=genome.ema_fast,
        ema_slow=genome.ema_slow,
        rsi_period=genome.rsi_period,
        rsi_long_max=genome.rsi_long_max,
        rsi_short_min=genome.rsi_short_min,
    )
    ast.parse(source)
    return source


def write_strategy(genome: StrategyGenome, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{genome.class_name}.py"
    path.write_text(compile_strategy(genome), encoding="utf-8")
    return path
