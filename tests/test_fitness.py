from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from strategy_miner.fitness import Metrics, passes_hard_filters, score  # noqa: E402


def make_metrics(**overrides) -> Metrics:
    base = {
        "total_return_pct": 20.0,
        "max_drawdown_pct": 10.0,
        "sharpe": 1.2,
        "sortino": 1.5,
        "profit_factor": 1.6,
        "expectancy": 0.5,
        "trades": 200,
        "pair_coverage": 0.8,
        "stability": 0.7,
        "complexity": 1.0,
    }
    base.update(overrides)
    return Metrics(**base)


class ScoreTests(unittest.TestCase):
    def test_results_are_finite_floats(self) -> None:
        result = score(make_metrics())
        self.assertIsInstance(result, float)
        self.assertTrue(math.isfinite(result))

    def test_higher_profit_factor_increases_score(self) -> None:
        low = score(make_metrics(profit_factor=1.2))
        high = score(make_metrics(profit_factor=2.5))
        self.assertGreater(high, low)

    def test_higher_drawdown_decreases_score(self) -> None:
        low_dd = score(make_metrics(max_drawdown_pct=5.0))
        high_dd = score(make_metrics(max_drawdown_pct=30.0))
        self.assertGreater(low_dd, high_dd)

    def test_higher_complexity_decreases_score(self) -> None:
        simple = score(make_metrics(complexity=1.0))
        complex_ = score(make_metrics(complexity=5.0))
        self.assertGreater(simple, complex_)


class HardFilterTests(unittest.TestCase):
    def base_kwargs(self) -> dict:
        return {
            "min_trades": 100,
            "max_drawdown_pct": 25.0,
            "min_profit_factor": 1.2,
            "min_pair_coverage": 0.5,
        }

    def test_all_conditions_met_passes(self) -> None:
        metrics = make_metrics(
            trades=100, max_drawdown_pct=25.0, profit_factor=1.2, expectancy=0.1, pair_coverage=0.5
        )
        self.assertTrue(passes_hard_filters(metrics, **self.base_kwargs()))

    def test_min_trades_exactly_met_passes(self) -> None:
        metrics = make_metrics(trades=100)
        self.assertTrue(passes_hard_filters(metrics, **self.base_kwargs()))

    def test_one_trade_below_min_fails(self) -> None:
        metrics = make_metrics(trades=99)
        self.assertFalse(passes_hard_filters(metrics, **self.base_kwargs()))

    def test_max_drawdown_exactly_met_passes(self) -> None:
        metrics = make_metrics(max_drawdown_pct=25.0)
        self.assertTrue(passes_hard_filters(metrics, **self.base_kwargs()))

    def test_drawdown_above_max_fails(self) -> None:
        metrics = make_metrics(max_drawdown_pct=25.01)
        self.assertFalse(passes_hard_filters(metrics, **self.base_kwargs()))

    def test_profit_factor_exactly_met_passes(self) -> None:
        metrics = make_metrics(profit_factor=1.2)
        self.assertTrue(passes_hard_filters(metrics, **self.base_kwargs()))

    def test_profit_factor_below_min_fails(self) -> None:
        metrics = make_metrics(profit_factor=1.19)
        self.assertFalse(passes_hard_filters(metrics, **self.base_kwargs()))

    def test_non_positive_expectancy_fails(self) -> None:
        for bad in (0.0, -0.5):
            with self.subTest(expectancy=bad):
                metrics = make_metrics(expectancy=bad)
                self.assertFalse(passes_hard_filters(metrics, **self.base_kwargs()))

    def test_min_expectancy_none_skips_expectancy_gate(self) -> None:
        metrics = make_metrics(expectancy=-5.0)
        self.assertTrue(
            passes_hard_filters(metrics, **self.base_kwargs(), min_expectancy=None)
        )

    def test_min_expectancy_threshold_is_strict(self) -> None:
        metrics = make_metrics(expectancy=-0.5)
        self.assertTrue(
            passes_hard_filters(metrics, **self.base_kwargs(), min_expectancy=-1.0)
        )
        self.assertFalse(
            passes_hard_filters(metrics, **self.base_kwargs(), min_expectancy=-0.5)
        )

    def test_pair_coverage_exactly_met_passes(self) -> None:
        metrics = make_metrics(pair_coverage=0.5)
        self.assertTrue(passes_hard_filters(metrics, **self.base_kwargs()))

    def test_pair_coverage_below_min_fails(self) -> None:
        metrics = make_metrics(pair_coverage=0.49)
        self.assertFalse(passes_hard_filters(metrics, **self.base_kwargs()))


class MetricDefaultTests(unittest.TestCase):
    def test_complexity_default_is_one(self) -> None:
        metrics = Metrics(
            total_return_pct=10.0,
            max_drawdown_pct=5.0,
            sharpe=1.0,
            sortino=1.0,
            profit_factor=1.5,
            expectancy=0.3,
            trades=150,
            pair_coverage=0.6,
            stability=0.5,
        )
        self.assertEqual(metrics.complexity, 1.0)


if __name__ == "__main__":
    unittest.main()
