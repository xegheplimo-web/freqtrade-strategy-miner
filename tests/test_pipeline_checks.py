"""Unit tests for the checks stages: bias (6), walk-forward (7) and final gate (9).

No docker, no network. Two Runner-protocol fakes are used:

- ``BiasRunner`` writes the lookahead CSV export (``has_bias`` True/False/absent) and
  returns the recursive-analysis stdout (fake table / garbage).
- ``BacktestRunner`` replays a script of per-call outcomes, writing a synthetic
  backtest zip (built from a minimal summary dict) into the results dir each time.
"""

from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path
from typing import Sequence

from strategy_miner.generator import GenomeGenerator
from strategy_miner.pipeline import make_context
from strategy_miner.pipeline.checks import (
    add_months,
    lookahead_args,
    parse_lookahead_csv,
    parse_recursive_stdout,
    recursive_args,
    run_bias_checks,
    run_final,
    run_walk_forward,
    walk_forward_windows,
)
from strategy_miner.runner import CONTAINER_USERDATA, RunResult

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_CONFIG = json.loads((REPO_ROOT / "config" / "miner.json").read_text(encoding="utf-8"))

# Fake recursive-analysis tables. RECURSIVE_TABLE max pct = 12.5% (> default 5.0);
# RECURSIVE_CLEAN max pct = 1.5% (< 5.0).
RECURSIVE_TABLE = (
    "Startup candle: 199\n"
    "| indicator | 199 | 399 |\n"
    "|-----------|-----|-----|\n"
    "| rsi | 0.000% | 12.500% |\n"
    "| ema | 3.000% | 1.000% |\n"
)
RECURSIVE_CLEAN = "| rsi | 0.000% | 1.500% |\n| ema | 0.500% | 0.000% |\n"

LOOKAHEAD_HEADER = [
    "filename", "strategy", "has_bias", "total_signals",
    "biased_entry_signals", "biased_exit_signals", "biased_indicators",
]


def _make_root(tmp_path: Path, *, config: dict | None = None) -> Path:
    """Create a minimal worktree: config/miner.json."""
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    cfg = SAMPLE_CONFIG if config is None else config
    (tmp_path / "config" / "miner.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return tmp_path


def _seed_candidates(ctx, count: int) -> dict[str, int]:
    """Write strategy files + upsert candidates; returns ``{class_name: id}``."""
    generator = GenomeGenerator(seed=1)
    ids: dict[str, int] = {}
    for i in range(1, count + 1):
        genome = generator.generate_one(i)
        py_path = ctx.generated_dir / f"{genome.class_name}.py"
        py_path.write_text("# generated strategy\n", encoding="utf-8")
        ids[genome.class_name] = ctx.store.upsert_candidate(
            genome, str(py_path.relative_to(ctx.root))
        )
    return ids


def _container_to_host(user_data: Path, container: str) -> Path:
    """Inverse of DockerRunner.map_path for a container path."""
    rel = container
    if rel.startswith(CONTAINER_USERDATA):
        rel = rel[len(CONTAINER_USERDATA):]
    return user_data / rel.lstrip("/")


def _make_backtest_summary(
    strategy_name: str,
    *,
    profit_factor: float = 1.2,
    expectancy: float = 1.0,
    total_trades: int = 500,
    max_drawdown_account: float = 0.1,
    pairlist: Sequence[str] | None = None,
    pairs_traded: int = 3,
    profit_total: float = 0.3,
) -> dict:
    """Minimal freqtrade summary with the fields ``extract_metrics`` needs."""
    pairs = list(pairlist) if pairlist is not None else [
        "A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT",
    ]
    per_pair = [
        {"key": pair, "trades": (total_trades // len(pairs)) if i < pairs_traded else 0}
        for i, pair in enumerate(pairs)
    ]
    per_pair.append({"key": "TOTAL", "trades": total_trades})
    months = [{"profit_abs": 1.0} for _ in range(6)] + [{"profit_abs": -1.0}]
    return {
        "strategy_name": strategy_name,
        "profit_total": profit_total,
        "max_drawdown_account": max_drawdown_account,
        "sharpe": 1.0,
        "sortino": 1.5,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "total_trades": total_trades,
        "results_per_pair": per_pair,
        "pairlist": pairs,
        "periodic_breakdown": {"month": months},
        "trades": [],
    }


def _write_backtest_zip(path: Path, strategy_name: str, summary: dict) -> Path:
    payload = {"strategy": {strategy_name: summary}, "strategy_comparison": []}
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"backtest-result-{strategy_name}.json", json.dumps(payload))
    return path


class BiasRunner:
    """Runner-protocol fake for the bias stage (CSV export + recursive stdout)."""

    def __init__(
        self,
        *,
        user_data: Path,
        lookahead_bias: dict[str, bool | None] | None = None,
        lookahead_exit: int = 0,
        recursive_exit: int = 0,
        recursive_stdout: dict[str, str] | None = None,
    ) -> None:
        self.user_data = Path(user_data)
        self.lookahead_bias = dict(lookahead_bias or {})
        self.lookahead_exit = lookahead_exit
        self.recursive_exit = recursive_exit
        self.recursive_stdout = dict(recursive_stdout or {})
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: int = 3600,
        log_dir: Path | None = None,
        log_name: str | None = None,
    ) -> RunResult:
        args = tuple(args)
        self.calls.append(args)
        class_name = args[args.index("--strategy") + 1]
        stdout = ""
        exit_code = 0
        if args[0] == "lookahead-analysis":
            exit_code = self.lookahead_exit
            bias = self.lookahead_bias.get(class_name)
            if exit_code == 0 and bias is not None:
                container = args[args.index("--lookahead-analysis-exportfilename") + 1]
                host = _container_to_host(self.user_data, container)
                host.parent.mkdir(parents=True, exist_ok=True)
                with host.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(LOOKAHEAD_HEADER)
                    writer.writerow([
                        f"{class_name}.py", class_name, bias, 120,
                        3 if bias else 0, 1 if bias else 0, "rsi" if bias else "",
                    ])
        elif args[0] == "recursive-analysis":
            exit_code = self.recursive_exit
            stdout = self.recursive_stdout.get(class_name, "")
        log_path = None
        if log_dir is not None and log_name is not None:
            log_path = Path(log_dir) / log_name
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(stdout or "log\n", encoding="utf-8")
        return RunResult(
            argv=args, exit_code=exit_code, stdout=stdout, duration_s=0.0, log_path=log_path
        )

    def map_path(self, host_path: Path) -> str:
        rel = Path(host_path).resolve().relative_to(self.user_data.resolve())
        return f"{CONTAINER_USERDATA}/{rel.as_posix()}"


class BacktestRunner:
    """Runner-protocol fake replaying a script of per-call backtest outcomes."""

    def __init__(
        self,
        *,
        results_dir: Path,
        user_data: Path,
        outcomes: Sequence[dict] | None = None,
    ) -> None:
        self.results_dir = Path(results_dir)
        self.user_data = Path(user_data)
        self.outcomes = [dict(outcome) for outcome in (outcomes or [{}])]
        self.calls: list[tuple[str, ...]] = []
        self._index = 0

    def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: int = 3600,
        log_dir: Path | None = None,
        log_name: str | None = None,
    ) -> RunResult:
        args = tuple(args)
        self.calls.append(args)
        outcome = self.outcomes[min(self._index, len(self.outcomes) - 1)]
        self._index += 1
        class_name = args[args.index("--strategy") + 1]
        exit_code = int(outcome.get("exit_code", 0))
        log_path = None
        if log_dir is not None and log_name is not None:
            log_path = Path(log_dir) / log_name
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("log\n", encoding="utf-8")
        if exit_code == 0 and outcome.get("artifact", True):
            self.results_dir.mkdir(parents=True, exist_ok=True)
            name = f"backtest-result-{class_name}-{self._index}.zip"
            summary = _make_backtest_summary(
                class_name,
                profit_factor=outcome.get("profit_factor", 1.2),
                expectancy=outcome.get("expectancy", 1.0),
                total_trades=outcome.get("total_trades", 500),
                max_drawdown_account=outcome.get("max_drawdown_account", 0.1),
                pairlist=outcome.get("pairlist"),
                pairs_traded=outcome.get("pairs_traded", 3),
                profit_total=outcome.get("profit_total", 0.3),
            )
            _write_backtest_zip(self.results_dir / name, class_name, summary)
            (self.results_dir / ".last_result.json").write_text(
                json.dumps({"latest_backtest": name}), encoding="utf-8"
            )
        return RunResult(
            argv=args, exit_code=exit_code, stdout="", duration_s=0.0, log_path=log_path
        )

    def map_path(self, host_path: Path) -> str:
        rel = Path(host_path).resolve().relative_to(self.user_data.resolve())
        return f"{CONTAINER_USERDATA}/{rel.as_posix()}"


def _bias_ctx(tmp_path: Path, **runner_kwargs):
    root = _make_root(tmp_path)
    user_data = root / "freqtrade" / "user_data"
    runner = BiasRunner(user_data=user_data, **runner_kwargs)
    ctx = make_context(root, runner=runner)
    ids = _seed_candidates(ctx, 1)
    return ctx, runner, ids


def _backtest_ctx(tmp_path: Path, *, outcomes: Sequence[dict] | None = None, count: int = 1):
    root = _make_root(tmp_path)
    user_data = root / "freqtrade" / "user_data"
    results_dir = user_data / "backtest_results"
    runner = BacktestRunner(results_dir=results_dir, user_data=user_data, outcomes=outcomes)
    ctx = make_context(root, runner=runner)
    ids = _seed_candidates(ctx, count)
    return ctx, runner, ids


class TestArgBuilders:
    def test_lookahead_args_exact(self, tmp_path: Path) -> None:
        ctx, _runner, _ids = _bias_ctx(tmp_path)
        args = lookahead_args(
            ctx, "Miner_000001", "20250101-20251231",
            "/freqtrade/user_data/analysis/lookahead_Miner_000001.csv",
        )
        assert args == [
            "lookahead-analysis",
            "--config", "/freqtrade/user_data/config.miner.json",
            "--strategy", "Miner_000001",
            "--strategy-path", "/freqtrade/user_data/strategies/generated",
            "--timerange", "20250101-20251231",
            "--lookahead-analysis-exportfilename",
            "/freqtrade/user_data/analysis/lookahead_Miner_000001.csv",
        ]

    def test_recursive_args_exact(self, tmp_path: Path) -> None:
        ctx, _runner, _ids = _bias_ctx(tmp_path)
        args = recursive_args(ctx, "Miner_000001", "20250101-20251231")
        assert args == [
            "recursive-analysis",
            "--config", "/freqtrade/user_data/config.miner.json",
            "--strategy", "Miner_000001",
            "--strategy-path", "/freqtrade/user_data/strategies/generated",
            "--timerange", "20250101-20251231",
        ]


class TestParseLookaheadCsv:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert parse_lookahead_csv(tmp_path / "nope.csv") is None

    def test_header_only_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.csv"
        path.write_text(",".join(LOOKAHEAD_HEADER) + "\n", encoding="utf-8")
        assert parse_lookahead_csv(path) is None

    def test_has_bias_true(self, tmp_path: Path) -> None:
        path = tmp_path / "x.csv"
        path.write_text(
            ",".join(LOOKAHEAD_HEADER) + "\n"
            "Miner_000001.py,Miner_000001,True,120,3,1,rsi\n",
            encoding="utf-8",
        )
        row = parse_lookahead_csv(path)
        assert row is not None
        assert row["strategy"] == "Miner_000001"
        assert row["has_bias"] is True
        assert row["total_signals"] == "120"

    def test_has_bias_false(self, tmp_path: Path) -> None:
        path = tmp_path / "x.csv"
        path.write_text(
            ",".join(LOOKAHEAD_HEADER) + "\n"
            "Miner_000001.py,Miner_000001,False,120,0,0,\n",
            encoding="utf-8",
        )
        row = parse_lookahead_csv(path)
        assert row is not None
        assert row["has_bias"] is False

    def test_has_bias_case_insensitive(self, tmp_path: Path) -> None:
        path = tmp_path / "x.csv"
        path.write_text(
            ",".join(LOOKAHEAD_HEADER) + "\n"
            "Miner_000001.py,Miner_000001,true,120,3,1,rsi\n",
            encoding="utf-8",
        )
        row = parse_lookahead_csv(path)
        assert row is not None
        assert row["has_bias"] is True


class TestParseRecursiveStdout:
    def test_multi_indicator_max_pct(self) -> None:
        assert parse_recursive_stdout(RECURSIVE_TABLE) == {"rsi": 12.5, "ema": 3.0}

    def test_whitespace_table(self) -> None:
        text = "rsi  0.000%  12.500%\nmfi 1.000% 2.000%\n"
        assert parse_recursive_stdout(text) == {"rsi": 12.5, "mfi": 2.0}

    def test_garbage_returns_empty(self) -> None:
        assert parse_recursive_stdout("no table here\njust prose about rsi\n") == {}

    def test_border_only_returns_empty(self) -> None:
        assert parse_recursive_stdout("|------|-----|\n|======|=====|\n") == {}

    def test_empty_string(self) -> None:
        assert parse_recursive_stdout("") == {}


class TestAddMonths:
    def test_month_end_clamp_non_leap(self) -> None:
        assert add_months("20220131", 1) == "20220228"

    def test_month_end_clamp_leap(self) -> None:
        assert add_months("20240131", 1) == "20240229"

    def test_simple_year(self) -> None:
        assert add_months("20220101", 12) == "20230101"

    def test_quarter(self) -> None:
        assert add_months("20250101", 3) == "20250401"

    def test_backwards(self) -> None:
        assert add_months("20220101", -1) == "20211201"


class TestWalkForwardWindows:
    def test_default_span_folds(self) -> None:
        folds = walk_forward_windows("20220101", "20251231")
        assert len(folds) == 11
        assert folds[0] == ("20220101", "20230101", "20230401")
        assert folds[1] == ("20220101", "20230401", "20230701")
        assert folds[-1] == ("20220101", "20250701", "20251001")

    def test_span_too_short_returns_empty(self) -> None:
        assert walk_forward_windows("20220101", "20230101") == []

    def test_single_fold_when_span_exactly_allows(self) -> None:
        assert walk_forward_windows("20220101", "20230401") == [
            ("20220101", "20230101", "20230401")
        ]

    def test_custom_step_and_test_lengths(self) -> None:
        folds = walk_forward_windows(
            "20220101", "20240101", train_months=12, test_months=1, step_months=1
        )
        assert len(folds) == 12
        assert folds[0] == ("20220101", "20230101", "20230201")
        assert folds[-1] == ("20220101", "20231201", "20240101")


class TestRunBiasChecks:
    def test_clean_passes_with_two_runs(self, tmp_path: Path) -> None:
        ctx, runner, ids = _bias_ctx(
            tmp_path,
            lookahead_bias={"Miner_000001": False},
            recursive_stdout={"Miner_000001": RECURSIVE_CLEAN},
        )
        cid = ids["Miner_000001"]
        results = run_bias_checks(ctx, [cid])

        assert results[cid] == "passed"
        assert ctx.store.stage_status(cid, "bias") == "passed"
        runs = ctx.store.runs_for(cid, "bias")
        assert len(runs) == 2
        assert runs[0]["argv"] == lookahead_args(
            ctx, "Miner_000001", "20250101-20251231",
            "/freqtrade/user_data/analysis/lookahead_Miner_000001.csv",
        )
        assert runs[1]["argv"] == recursive_args(ctx, "Miner_000001", "20250101-20251231")
        assert runs[1]["metrics"] == {"rsi": 1.5, "ema": 0.5}
        assert runner.calls[0] == tuple(runs[0]["argv"])
        assert runner.calls[1] == tuple(runs[1]["argv"])
        # lookahead CSV written into <user_data>/analysis
        assert (ctx.user_data / "analysis" / "lookahead_Miner_000001.csv").is_file()

    def test_lookahead_bias_rejected(self, tmp_path: Path) -> None:
        ctx, _runner, ids = _bias_ctx(
            tmp_path,
            lookahead_bias={"Miner_000001": True},
            recursive_stdout={"Miner_000001": RECURSIVE_CLEAN},
        )
        cid = ids["Miner_000001"]
        results = run_bias_checks(ctx, [cid])

        assert results[cid] == "rejected: lookahead bias"
        assert ctx.store.stage_status(cid, "bias") == "rejected"
        assert len(ctx.store.runs_for(cid, "bias")) == 2

    def test_inconclusive_when_no_csv_row(self, tmp_path: Path) -> None:
        ctx, _runner, ids = _bias_ctx(
            tmp_path,
            lookahead_bias={"Miner_000001": None},
            recursive_stdout={"Miner_000001": RECURSIVE_CLEAN},
        )
        cid = ids["Miner_000001"]
        results = run_bias_checks(ctx, [cid])

        assert results[cid] == "rejected: bias test inconclusive"
        assert ctx.store.stage_status(cid, "bias") == "rejected"
        assert not (ctx.user_data / "analysis" / "lookahead_Miner_000001.csv").exists()

    def test_recursive_over_threshold_rejected(self, tmp_path: Path) -> None:
        ctx, _runner, ids = _bias_ctx(
            tmp_path,
            lookahead_bias={"Miner_000001": False},
            recursive_stdout={"Miner_000001": RECURSIVE_TABLE},
        )
        cid = ids["Miner_000001"]
        results = run_bias_checks(ctx, [cid])

        assert results[cid] == "rejected: recursive rsi 12.5%"
        assert ctx.store.stage_status(cid, "bias") == "rejected"

    def test_lookahead_failure(self, tmp_path: Path) -> None:
        ctx, _runner, ids = _bias_ctx(
            tmp_path,
            lookahead_exit=2,
            recursive_stdout={"Miner_000001": RECURSIVE_CLEAN},
        )
        cid = ids["Miner_000001"]
        results = run_bias_checks(ctx, [cid])

        assert results[cid] == "failed: 2"
        assert ctx.store.stage_status(cid, "bias") == "failed"
        assert len(ctx.store.runs_for(cid, "bias")) == 2

    def test_recursive_failure(self, tmp_path: Path) -> None:
        ctx, _runner, ids = _bias_ctx(
            tmp_path,
            lookahead_bias={"Miner_000001": False},
            recursive_exit=3,
        )
        cid = ids["Miner_000001"]
        results = run_bias_checks(ctx, [cid])

        assert results[cid] == "failed: 3"
        assert ctx.store.stage_status(cid, "bias") == "failed"

    def test_recursive_garbage_stdout_passes(self, tmp_path: Path) -> None:
        ctx, _runner, ids = _bias_ctx(
            tmp_path,
            lookahead_bias={"Miner_000001": False},
            recursive_stdout={"Miner_000001": "unparsable prose, no percentages\n"},
        )
        cid = ids["Miner_000001"]
        results = run_bias_checks(ctx, [cid])

        assert results[cid] == "passed"
        runs = ctx.store.runs_for(cid, "bias")
        assert "unparsed output" in (runs[1]["notes"] or "")

    def test_custom_bias_config_timerange_and_threshold(self, tmp_path: Path) -> None:
        cfg = dict(SAMPLE_CONFIG)
        cfg["bias"] = {"timerange": "20250101-20250630", "recursive_max_pct": 20.0}
        root = _make_root(tmp_path, config=cfg)
        user_data = root / "freqtrade" / "user_data"
        runner = BiasRunner(
            user_data=user_data,
            lookahead_bias={"Miner_000001": False},
            recursive_stdout={"Miner_000001": RECURSIVE_TABLE},
        )
        ctx = make_context(root, runner=runner)
        cid = _seed_candidates(ctx, 1)["Miner_000001"]

        results = run_bias_checks(ctx, [cid])
        assert results[cid] == "passed"  # 12.5% < 20.0
        assert runner.calls[0] == tuple(lookahead_args(
            ctx, "Miner_000001", "20250101-20250630",
            "/freqtrade/user_data/analysis/lookahead_Miner_000001.csv",
        ))

    def test_multiple_candidates(self, tmp_path: Path) -> None:
        root = _make_root(tmp_path)
        user_data = root / "freqtrade" / "user_data"
        runner = BiasRunner(
            user_data=user_data,
            lookahead_bias={"Miner_000001": False, "Miner_000002": True},
            recursive_stdout={"Miner_000001": RECURSIVE_CLEAN, "Miner_000002": RECURSIVE_CLEAN},
        )
        ctx = make_context(root, runner=runner)
        ids = _seed_candidates(ctx, 2)

        results = run_bias_checks(ctx, [ids["Miner_000001"], ids["Miner_000002"]])
        assert results[ids["Miner_000001"]] == "passed"
        assert results[ids["Miner_000002"]] == "rejected: lookahead bias"


class TestRunWalkForward:
    def test_all_folds_positive_passes(self, tmp_path: Path) -> None:
        outcomes = [{"profit_factor": 1.5, "expectancy": 1.0} for _ in range(4)]
        ctx, runner, ids = _backtest_ctx(tmp_path, outcomes=outcomes)
        cid = ids["Miner_000001"]

        results = run_walk_forward(ctx, [cid], max_folds=4)

        assert results[cid] == "passed"
        assert ctx.store.stage_status(cid, "walk_forward") == "passed"
        runs = ctx.store.runs_for(cid, "walk_forward")
        assert len(runs) == 4
        # first fold window = 20230101-20230401 (train_start 20220101 + 12 months)
        assert runs[0]["notes"] == "20230101-20230401"
        assert runner.calls[0][runner.calls[0].index("--timerange") + 1] == "20230101-20230401"
        assert runs[0]["metrics"]["profit_factor"] == 1.5

    def test_low_median_pf_rejected(self, tmp_path: Path) -> None:
        outcomes = [{"profit_factor": 0.5, "expectancy": 1.0} for _ in range(4)]
        ctx, _runner, ids = _backtest_ctx(tmp_path, outcomes=outcomes)
        cid = ids["Miner_000001"]

        results = run_walk_forward(ctx, [cid], max_folds=4)

        assert results[cid] == "rejected: median profit_factor 0.5 < 1.0"
        assert ctx.store.stage_status(cid, "walk_forward") == "rejected"

    def test_median_exactly_one_passes(self, tmp_path: Path) -> None:
        outcomes = [
            {"profit_factor": 0.5, "expectancy": 1.0},
            {"profit_factor": 1.5, "expectancy": 1.0},
        ]
        ctx, _runner, ids = _backtest_ctx(tmp_path, outcomes=outcomes)
        cid = ids["Miner_000001"]

        results = run_walk_forward(ctx, [cid], max_folds=2)
        assert results[cid] == "passed"

    def test_negative_expectancy_fraction_rejected(self, tmp_path: Path) -> None:
        outcomes = [
            {"profit_factor": 1.5, "expectancy": 1.0},
            {"profit_factor": 1.5, "expectancy": -1.0},
            {"profit_factor": 1.5, "expectancy": -1.0},
            {"profit_factor": 1.5, "expectancy": -1.0},
        ]
        ctx, _runner, ids = _backtest_ctx(tmp_path, outcomes=outcomes)
        cid = ids["Miner_000001"]

        results = run_walk_forward(ctx, [cid], max_folds=4)

        assert results[cid] == "rejected: positive expectancy fraction 0.25 < 0.6"
        assert ctx.store.stage_status(cid, "walk_forward") == "rejected"

    def test_too_many_fold_failures(self, tmp_path: Path) -> None:
        outcomes = [
            {"exit_code": 1},
            {"exit_code": 1},
            {"profit_factor": 1.5, "expectancy": 1.0},
            {"profit_factor": 1.5, "expectancy": 1.0},
        ]
        ctx, _runner, ids = _backtest_ctx(tmp_path, outcomes=outcomes)
        cid = ids["Miner_000001"]

        results = run_walk_forward(ctx, [cid], max_folds=4)

        assert results[cid] == "failed: 2/4 folds failed"
        assert ctx.store.stage_status(cid, "walk_forward") == "failed"
        # every fold attempt is recorded (successful + failed).
        assert len(ctx.store.runs_for(cid, "walk_forward")) == 4

    def test_one_of_four_failures_is_tolerated(self, tmp_path: Path) -> None:
        outcomes = [
            {"exit_code": 1},
            {"profit_factor": 1.5, "expectancy": 1.0},
            {"profit_factor": 1.5, "expectancy": 1.0},
            {"profit_factor": 1.5, "expectancy": 1.0},
        ]
        ctx, _runner, ids = _backtest_ctx(tmp_path, outcomes=outcomes)
        cid = ids["Miner_000001"]

        results = run_walk_forward(ctx, [cid], max_folds=4)
        assert results[cid] == "passed"

    def test_no_windows_fails(self, tmp_path: Path) -> None:
        cfg = dict(SAMPLE_CONFIG)
        cfg["validation_timerange"] = "20230101-20230101"  # no room for a fold
        root = _make_root(tmp_path, config=cfg)
        user_data = root / "freqtrade" / "user_data"
        runner = BacktestRunner(
            results_dir=user_data / "backtest_results", user_data=user_data
        )
        ctx = make_context(root, runner=runner)
        cid = _seed_candidates(ctx, 1)["Miner_000001"]

        results = run_walk_forward(ctx, [cid])
        assert results[cid] == "failed: no walk-forward windows"
        assert ctx.store.stage_status(cid, "walk_forward") == "failed"


def _freeze(ctx, class_name: str) -> None:
    """Write a params sidecar next to the strategy file (marks it frozen)."""
    (ctx.generated_dir / f"{class_name}.json").write_text(
        json.dumps({"strategy_name": class_name, "params": {}, "ft_stratparam_v": 1}),
        encoding="utf-8",
    )


class TestRunFinal:
    def test_not_frozen_rejected(self, tmp_path: Path) -> None:
        ctx, runner, ids = _backtest_ctx(tmp_path, outcomes=[{}])
        cid = ids["Miner_000001"]

        results = run_final(ctx, [cid])

        assert results[cid] == "rejected: not frozen (no params file)"
        assert ctx.store.stage_status(cid, "final_test") == "rejected"
        assert runner.calls == []
        assert ctx.store.runs_for(cid, "final_test") == []

    def test_pass_writes_champion(self, tmp_path: Path) -> None:
        ctx, _runner, ids = _backtest_ctx(
            tmp_path, outcomes=[{"profit_factor": 1.5, "expectancy": 2.0}]
        )
        cid = ids["Miner_000001"]
        _freeze(ctx, "Miner_000001")

        results = run_final(ctx, [cid])

        assert results[cid] == "passed"
        assert ctx.store.stage_status(cid, "final_test") == "passed"

        champion_dir = ctx.root / "output" / "champions" / "Miner_000001"
        assert (champion_dir / "Miner_000001.py").is_file()
        assert (champion_dir / "Miner_000001.json").is_file()
        assert len(list(champion_dir.glob("backtest-result-*.zip"))) == 1

        manifest = json.loads((champion_dir / "champion.json").read_text(encoding="utf-8"))
        assert manifest["class_name"] == "Miner_000001"
        assert manifest["timerange"] == "20260101-20260930"
        assert manifest["metrics"]["profit_factor"] == 1.5
        assert manifest["metrics"]["expectancy"] == 2.0
        assert "created_at" in manifest

        runs = ctx.store.runs_for(cid, "final_test")
        assert len(runs) == 1
        assert runs[0]["argv"][runs[0]["argv"].index("--timerange") + 1] == "20260101-20260930"
        assert manifest["run_ids"]["final_test"] == runs[0]["id"]

    def test_one_shot_skip_then_force(self, tmp_path: Path) -> None:
        ctx, runner, ids = _backtest_ctx(
            tmp_path, outcomes=[{"profit_factor": 1.5, "expectancy": 2.0}]
        )
        cid = ids["Miner_000001"]
        _freeze(ctx, "Miner_000001")

        assert run_final(ctx, [cid])[cid] == "passed"
        second = run_final(ctx, [cid])
        assert second[cid] == "skipped: already tested (use force)"
        assert ctx.store.stage_status(cid, "final_test") == "skipped"
        assert len(runner.calls) == 1

        forced = run_final(ctx, [cid], force=True)
        assert forced[cid] == "passed"
        assert len(runner.calls) == 2

    def test_reject_on_hard_filters(self, tmp_path: Path) -> None:
        ctx, _runner, ids = _backtest_ctx(
            tmp_path, outcomes=[{"profit_factor": 0.5, "expectancy": 2.0}]
        )
        cid = ids["Miner_000001"]
        _freeze(ctx, "Miner_000001")

        results = run_final(ctx, [cid])

        assert results[cid].startswith("rejected:")
        assert "profit_factor" in results[cid]
        assert ctx.store.stage_status(cid, "final_test") == "rejected"
        assert not (ctx.root / "output" / "champions" / "Miner_000001").exists()

    def test_reject_on_negative_expectancy(self, tmp_path: Path) -> None:
        ctx, _runner, ids = _backtest_ctx(
            tmp_path, outcomes=[{"profit_factor": 1.5, "expectancy": -0.5}]
        )
        cid = ids["Miner_000001"]
        _freeze(ctx, "Miner_000001")

        results = run_final(ctx, [cid])
        assert results[cid] == "rejected: expectancy <= 0"

    def test_backtest_failure(self, tmp_path: Path) -> None:
        ctx, _runner, ids = _backtest_ctx(tmp_path, outcomes=[{"exit_code": 1}])
        cid = ids["Miner_000001"]
        _freeze(ctx, "Miner_000001")

        results = run_final(ctx, [cid])

        assert results[cid] == "failed: 1"
        assert ctx.store.stage_status(cid, "final_test") == "failed"
        assert len(ctx.store.runs_for(cid, "final_test")) == 1

