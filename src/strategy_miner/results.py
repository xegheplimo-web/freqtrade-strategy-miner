"""Parse freqtrade backtest artifacts (zip or extracted json) into StrategyStats.

FROZEN contract: .orchestrator/INTERFACES.md section 2 (2026-10-08).
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from strategy_miner import fitness


@dataclass(frozen=True)
class StrategyStats:
    strategy_name: str
    summary: dict[str, Any]
    trades: tuple[dict[str, Any], ...]
    per_pair: tuple[dict[str, Any], ...]
    pairlist: tuple[str, ...]


def _parse_json_bytes(payload: bytes, source: Path) -> dict:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed backtest json in {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"backtest json in {source} is not a JSON object")
    return data


def _load_from_zip(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as zf:
            names = [
                n
                for n in zf.namelist()
                if n.endswith(".json") and not n.endswith("_config.json")
            ]
            if not names:
                raise ValueError(f"no backtest result json found in zip {path}")
            preferred = [n for n in names if Path(n).name.startswith("backtest-result-")]
            name = sorted(preferred)[0] if preferred else sorted(names)[0]
            payload = zf.read(name)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"unreadable backtest zip {path}: {exc}") from exc
    return _parse_json_bytes(payload, path)


def _load_from_json_file(path: Path) -> dict:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"unreadable backtest json {path}: {exc}") from exc
    return _parse_json_bytes(payload, path)


def load_backtest_json(path: Path) -> dict:
    """Load a backtest result: .zip export or plain .json file."""
    path = Path(path)
    if path.suffix.lower() == ".zip":
        return _load_from_zip(path)
    return _load_from_json_file(path)


def parse_backtest_result(path: Path) -> dict[str, StrategyStats]:
    """Parse a backtest artifact into {strategy_name: StrategyStats}."""
    data = load_backtest_json(path)
    strategies = data.get("strategy")
    if not isinstance(strategies, dict) or not strategies:
        raise ValueError(f"no strategies found in {path}")
    parsed: dict[str, StrategyStats] = {}
    for name, summary in strategies.items():
        if not isinstance(summary, dict):
            raise ValueError(f"strategy {name!r} in {path} is not an object")
        raw_per_pair = summary.get("results_per_pair") or []
        parsed[name] = StrategyStats(
            strategy_name=name,
            summary=summary,
            trades=tuple(summary.get("trades") or []),
            per_pair=tuple(row for row in raw_per_pair if row.get("key") != "TOTAL"),
            pairlist=tuple(summary.get("pairlist") or []),
        )
    return parsed


def positive_months_fraction(summary: dict) -> float:
    """Fraction of months (periodic_breakdown.month) with profit_abs > 0."""
    breakdown = summary.get("periodic_breakdown") or {}
    months = breakdown.get("month") or []
    if not months:
        return 0.0
    positive = sum(1 for m in months if (m.get("profit_abs") or 0) > 0)
    return positive / len(months)


def extract_metrics(stats: StrategyStats) -> fitness.Metrics:
    """Map a StrategyStats summary onto fitness.Metrics (FROZEN mapping)."""
    summary = stats.summary
    traded_pairs = sum(1 for row in stats.per_pair if (row.get("trades") or 0) > 0)
    pair_coverage = traded_pairs / len(stats.pairlist) if stats.pairlist else 0.0
    return fitness.Metrics(
        total_return_pct=summary["profit_total"] * 100,
        max_drawdown_pct=summary["max_drawdown_account"] * 100,
        sharpe=summary["sharpe"],
        sortino=summary["sortino"],
        profit_factor=summary["profit_factor"],
        expectancy=summary["expectancy"],
        trades=summary["total_trades"],
        pair_coverage=pair_coverage,
        stability=positive_months_fraction(summary),
        complexity=1.0,
    )


def drop_top_n_trades(trades: Sequence[dict], n: int) -> dict:
    """Drop the n most profitable trades; return aggregate stats of the rest."""
    ordered = sorted(trades, key=lambda t: t.get("profit_abs", 0.0), reverse=True)
    remaining = list(ordered[n:])
    profit_abs_after = float(sum(t.get("profit_abs", 0.0) for t in remaining))
    gross_profit = sum(t["profit_abs"] for t in remaining if t.get("profit_abs", 0.0) > 0)
    gross_loss = sum(t["profit_abs"] for t in remaining if t.get("profit_abs", 0.0) < 0)
    profit_factor_after = gross_profit / abs(gross_loss) if gross_loss != 0 else None
    return {
        "trades_after": len(remaining),
        "profit_abs_after": profit_abs_after,
        "profit_factor_after": profit_factor_after,
    }
