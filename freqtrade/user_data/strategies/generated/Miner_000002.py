from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta


class Miner_000002(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = '5m'
    can_short = True
    process_only_new_candles = True
    startup_candle_count = 300

    minimal_roi = {"0": 0.10, "60": 0.04, "180": 0.0}
    stoploss = -0.186
    trailing_stop = False
    use_exit_signal = True

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=10)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=238)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=13)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        long_condition = (
            (dataframe["ema_fast"] > dataframe["ema_slow"]) &
            (dataframe["rsi"] < 31) &
            (dataframe["volume"] > 0)
        )
        dataframe.loc[long_condition, ["enter_long", "enter_tag"]] = (1, "trend_rsi_long")

        if self.can_short:
            short_condition = (
                (dataframe["ema_fast"] < dataframe["ema_slow"]) &
                (dataframe["rsi"] > 76) &
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
