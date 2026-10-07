"""Unit tests for strategy_miner.results against the real fixture export."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from strategy_miner import fitness
from strategy_miner.results import (
    StrategyStats,
    drop_top_n_trades,
    extract_metrics,
    load_backtest_json,
    parse_backtest_result,
    positive_months_fraction,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "backtest-sample.zip"
RESULT_MEMBER = "backtest-result-2026-10-07_13-46-17.json"


@pytest.fixture(scope="module")
def parsed() -> dict[str, StrategyStats]:
    return parse_backtest_result(FIXTURE)


def _minimal_strategy(name: str) -> dict[str, Any]:
    return {
        "strategy_name": name,
        "total_trades": 2,
        "profit_total": 0.01,
        "profit_total_abs": 10.0,
        "max_drawdown_account": 0.05,
        "sharpe": 1.5,
        "sortino": 2.0,
        "profit_factor": 1.4,
        "expectancy": 0.3,
        "trades": [
            {"profit_abs": 6.0, "open_date": "2026-01-01 00:00:00+00:00"},
            {"profit_abs": 4.0, "open_date": "2026-01-02 00:00:00+00:00"},
        ],
        "results_per_pair": [
            {"key": "AAA/USDT:USDT", "trades": 2, "profit_abs": 10.0},
            {"key": "TOTAL", "trades": 2, "profit_abs": 10.0},
        ],
        "pairlist": ["AAA/USDT:USDT"],
        "periodic_breakdown": {"month": [{"profit_abs": 10.0}]},
    }


class TestParseBacktestResult:
    def test_fixture_single_strategy(self, parsed: dict[str, StrategyStats]) -> None:
        assert set(parsed) == {"ZarTest02SL35"}

    def test_trades_count_and_first_open_date(self, parsed: dict[str, StrategyStats]) -> None:
        stats = parsed["ZarTest02SL35"]
        assert len(stats.trades) == 4705
        assert stats.trades[0]["open_date"].startswith("2026-09-07")

    def test_per_pair_excludes_total(self, parsed: dict[str, StrategyStats]) -> None:
        stats = parsed["ZarTest02SL35"]
        assert len(stats.per_pair) == 23
        assert all(row.get("key") != "TOTAL" for row in stats.per_pair)

    def test_pairlist(self, parsed: dict[str, StrategyStats]) -> None:
        assert len(parsed["ZarTest02SL35"].pairlist) == 23

    def test_summary_pins(self, parsed: dict[str, StrategyStats]) -> None:
        summary = parsed["ZarTest02SL35"].summary
        assert summary["total_trades"] == 4705
        assert summary["profit_factor"] == pytest.approx(1.2132739626062352, abs=1e-12)
        assert summary["profit_total"] == pytest.approx(3.17402467726923, abs=1e-12)
        assert summary["max_drawdown_account"] == pytest.approx(0.14730292549816232, abs=1e-12)


class TestExtractMetrics:
    def test_basic_mapping(self, parsed: dict[str, StrategyStats]) -> None:
        stats = parsed["ZarTest02SL35"]
        m = extract_metrics(stats)
        assert isinstance(m, fitness.Metrics)
        assert m.trades == 4705
        assert m.total_return_pct == pytest.approx(317.402467726923, abs=1e-9)
        assert m.max_drawdown_pct == pytest.approx(14.730292549816232, abs=1e-9)
        assert m.sharpe == pytest.approx(stats.summary["sharpe"])
        assert m.sortino == pytest.approx(stats.summary["sortino"])
        assert m.profit_factor == pytest.approx(stats.summary["profit_factor"])
        assert m.expectancy == pytest.approx(stats.summary["expectancy"])

    def test_stability_and_complexity(self, parsed: dict[str, StrategyStats]) -> None:
        m = extract_metrics(parsed["ZarTest02SL35"])
        assert m.stability == 1.0
        assert m.complexity == 1.0

    def test_pair_coverage(self, parsed: dict[str, StrategyStats]) -> None:
        stats = parsed["ZarTest02SL35"]
        m = extract_metrics(stats)
        expected = sum(1 for r in stats.per_pair if r.get("trades", 0) > 0) / len(
            stats.pairlist
        )
        assert m.pair_coverage == pytest.approx(expected)
        assert 0 < m.pair_coverage <= 1


class TestPositiveMonthsFraction:
    def test_missing_breakdown(self) -> None:
        assert positive_months_fraction({}) == 0.0
        assert positive_months_fraction({"periodic_breakdown": {}}) == 0.0

    def test_empty_month_list(self) -> None:
        assert positive_months_fraction({"periodic_breakdown": {"month": []}}) == 0.0

    def test_mixed(self) -> None:
        summary = {
            "periodic_breakdown": {
                "month": [{"profit_abs": 1.0}, {"profit_abs": -2.0}, {"profit_abs": 0.5}]
            }
        }
        assert positive_months_fraction(summary) == pytest.approx(2 / 3)

    def test_zero_profit_is_not_positive(self) -> None:
        summary = {
            "periodic_breakdown": {"month": [{"profit_abs": 0.0}, {"profit_abs": -1.0}]}
        }
        assert positive_months_fraction(summary) == 0.0


class TestDropTopNTrades:
    def test_zero_drops_everything(self, parsed: dict[str, StrategyStats]) -> None:
        result = drop_top_n_trades(parsed["ZarTest02SL35"].trades, 0)
        assert result["trades_after"] == 4705
        assert result["profit_abs_after"] == pytest.approx(8252.4641609, abs=1e-6)
        assert result["profit_factor_after"] is not None

    def test_drop_100(self, parsed: dict[str, StrategyStats]) -> None:
        trades = parsed["ZarTest02SL35"].trades
        result = drop_top_n_trades(trades, 100)
        assert result["trades_after"] == 4605
        assert result["profit_abs_after"] < 8252.4641609
        remaining = sorted(trades, key=lambda t: t["profit_abs"], reverse=True)[100:]
        assert result["profit_abs_after"] == pytest.approx(
            sum(t["profit_abs"] for t in remaining), abs=1e-6
        )

    def test_drop_more_than_available(self, parsed: dict[str, StrategyStats]) -> None:
        result = drop_top_n_trades(parsed["ZarTest02SL35"].trades, 10**6)
        assert result["trades_after"] == 0
        assert result["profit_abs_after"] == 0.0
        assert result["profit_factor_after"] is None


class TestLoadBacktestJson:
    def test_zip(self) -> None:
        data = load_backtest_json(FIXTURE)
        assert set(data) >= {"strategy", "strategy_comparison"}
        assert "ZarTest02SL35" in data["strategy"]

    def test_extracted_json(self, tmp_path: Path) -> None:
        with zipfile.ZipFile(FIXTURE) as zf:
            payload = zf.read(RESULT_MEMBER)
        target = tmp_path / RESULT_MEMBER
        target.write_bytes(payload)
        data = load_backtest_json(target)
        assert "ZarTest02SL35" in data["strategy"]

    def test_malformed_zip(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"this is not a zip")
        with pytest.raises(ValueError):
            load_backtest_json(bad)

    def test_malformed_json(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError):
            load_backtest_json(bad)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            load_backtest_json(tmp_path / "missing.json")

    def test_zip_without_result_json(self, tmp_path: Path) -> None:
        zpath = tmp_path / "empty.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("backtest-result-2026-01-01_config.json", "{}")
        with pytest.raises(ValueError):
            load_backtest_json(zpath)


class TestSynthetic:
    def test_two_strategy_json(self, tmp_path: Path) -> None:
        payload = {
            "strategy": {
                "AlphaStrat": _minimal_strategy("AlphaStrat"),
                "BetaStrat": _minimal_strategy("BetaStrat"),
            }
        }
        target = tmp_path / "synthetic.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        parsed = parse_backtest_result(target)
        assert set(parsed) == {"AlphaStrat", "BetaStrat"}
        assert parsed["AlphaStrat"].per_pair[0]["key"] == "AAA/USDT:USDT"
        assert parsed["BetaStrat"].trades[0]["profit_abs"] == 6.0
        assert parsed["AlphaStrat"].pairlist == ("AAA/USDT:USDT",)


