from __future__ import annotations

from pathlib import Path


def _q(path: Path | str) -> str:
    return f'"{path}"'


def backtest_command(
    strategy: str,
    config: Path,
    strategy_path: Path,
    timerange: str,
) -> str:
    return (
        f"freqtrade backtesting --config {_q(config)} "
        f"--strategy {strategy} --strategy-path {_q(strategy_path)} "
        f"--timerange {timerange} --export trades"
    )


def hyperopt_command(
    strategy: str,
    config: Path,
    strategy_path: Path,
    timerange: str,
    epochs: int = 300,
) -> str:
    return (
        f"freqtrade hyperopt --config {_q(config)} "
        f"--strategy {strategy} --strategy-path {_q(strategy_path)} "
        f"--timerange {timerange} --spaces buy roi stoploss "
        f"--hyperopt-loss MultiMetricHyperOptLoss -e {epochs}"
    )


def lookahead_command(strategy: str, config: Path, strategy_path: Path, timerange: str) -> str:
    return (
        f"freqtrade lookahead-analysis --config {_q(config)} "
        f"--strategy {strategy} --strategy-path {_q(strategy_path)} "
        f"--timerange {timerange}"
    )


def recursive_command(strategy: str, config: Path, strategy_path: Path, timerange: str) -> str:
    return (
        f"freqtrade recursive-analysis --config {_q(config)} "
        f"--strategy {strategy} --strategy-path {_q(strategy_path)} "
        f"--timerange {timerange}"
    )
