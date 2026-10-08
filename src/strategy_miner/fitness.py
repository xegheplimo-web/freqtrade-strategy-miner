from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Metrics:
    total_return_pct: float
    max_drawdown_pct: float
    sharpe: float
    sortino: float
    profit_factor: float
    expectancy: float
    trades: int
    pair_coverage: float
    stability: float
    complexity: float = 1.0


def score(metrics: Metrics) -> float:
    """Illustrative composite fitness. Recalibrate before production use."""
    trade_quality = min(metrics.trades / 1000.0, 2.0)
    return (
        0.20 * metrics.total_return_pct
        + 8.0 * metrics.sharpe
        + 5.0 * metrics.sortino
        + 12.0 * metrics.profit_factor
        + 20.0 * metrics.expectancy
        + 12.0 * metrics.pair_coverage
        + 12.0 * metrics.stability
        + 5.0 * trade_quality
        - 0.75 * metrics.max_drawdown_pct
        - 2.0 * metrics.complexity
    )


def passes_hard_filters(
    metrics: Metrics,
    *,
    min_trades: int,
    max_drawdown_pct: float,
    min_profit_factor: float,
    min_pair_coverage: float,
    min_expectancy: float | None = 0.0,
) -> bool:
    """True when every configured gate holds.

    ``min_expectancy`` defaults to ``0.0`` (expectancy must be strictly
    positive — the historical behavior). Pass ``None`` to skip the check; the
    pre-hyperopt fast-gate screen uses that because refining weak default
    parameter sets is the whole point of hyperopt (DECISIONS D-017).
    """
    return (
        metrics.trades >= min_trades
        and metrics.max_drawdown_pct <= max_drawdown_pct
        and metrics.profit_factor >= min_profit_factor
        and (min_expectancy is None or metrics.expectancy > min_expectancy)
        and metrics.pair_coverage >= min_pair_coverage
    )
