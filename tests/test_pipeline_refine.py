"""Unit tests for the refine stages: hyperopt + validation (no docker, no network).

FakeRunner implements the Runner protocol and dispatches on ``args[0]``:

- ``"hyperopt"`` -> writes the params sidecar ``<generated>/<Class>.json`` via
  ``params.write_params_file`` (valid freqtrade format) unless configured not
  to, then exits 0.
- ``"backtesting"`` -> writes a synthetic ``backtest-result-*.zip`` (built by
  ``make_result_zip``) into the results dir and points ``.last_result.json``
  at it, then exits 0.
- any class in ``fail_for`` -> exit 1 and no artifacts (the failure variant).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from strategy_miner import params
from strategy_miner.genome import StrategyGenome
from strategy_miner.pipeline import backtest_args, make_context
from strategy_miner.pipeline.refine import (
    hyperopt_args,
    refine_pipeline,
    run_hyperopt,
    run_validation,
)
from strategy_miner.runner import CONTAINER_USERDATA, RunResult

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_CONFIG = json.loads((REPO_ROOT / "config" / "miner.json").read_text(encoding="utf-8"))

PAIRS = ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT")

# A validation spec that passes every gate when no train metrics exist.
GOOD_SPEC = {
    "profit_total": 0.10,
    "max_drawdown_account": 0.05,
    "profit_factor": 1.5,
    "expectancy": 0.5,
    "total_trades": 400,
}


def make_result_zip(
    dir_path: Path,
    strategy_name: str,
    *,
    profit_total: float,
    max_drawdown_account: float,
    profit_factor: float,
    expectancy: float,
    total_trades: int,
    per_pair_trades: Mapping[str, int] | None = None,
) -> Path:
    """Write a minimal synthetic backtest-result zip and return its path.

    ``results.parse`` tolerates the missing optional keys; ``results_per_pair``
    ends with the required ``TOTAL`` row and ``pairlist`` holds the pair keys.
    """
    if per_pair_trades is None:
        per_pair_trades = {pair: total_trades // len(PAIRS) for pair in PAIRS}
    results_per_pair = [
        {"key": pair, "trades": trades} for pair, trades in per_pair_trades.items()
    ]
    results_per_pair.append({"key": "TOTAL", "trades": total_trades})
    summary = {
        "profit_total": profit_total,
        "max_drawdown_account": max_drawdown_account,
        "sharpe": 1.0,
        "sortino": 1.2,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "total_trades": total_trades,
        "trades": [],
        "results_per_pair": results_per_pair,
        "pairlist": list(per_pair_trades),
    }
    payload = {"strategy": {strategy_name: summary}, "strategy_comparison": []}
    dir_path = Path(dir_path)
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / f"backtest-result-{strategy_name}.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"backtest-result-{strategy_name}.json", json.dumps(payload))
    return path


class FakeRunner:
    """Runner-protocol fake covering the hyperopt / validation / failure variants."""

    def __init__(
        self,
        *,
        generated_dir: Path,
        results_dir: Path,
        user_data: Path,
        fail_for: Iterable[str] = (),
        write_sidecar: bool = True,
        sidecar_params: Mapping[str, dict] | None = None,
        sidecar_text: Mapping[str, str] | None = None,
        zip_specs: Mapping[str, dict] | None = None,
        default_zip_spec: Mapping | None = None,
        write_artifact: bool = True,
    ) -> None:
        self.generated_dir = Path(generated_dir)
        self.results_dir = Path(results_dir)
        self.user_data = Path(user_data)
        self.fail_for = frozenset(fail_for)
        self.write_sidecar = write_sidecar
        self.sidecar_params = dict(sidecar_params or {})
        self.sidecar_text = dict(sidecar_text or {})
        self.zip_specs = dict(zip_specs or {})
        self.default_zip_spec = dict(default_zip_spec or GOOD_SPEC)
        self.write_artifact = write_artifact
        self.calls: list[tuple[tuple[str, ...], Path | None, str | None]] = []

    def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: int = 3600,
        log_dir: Path | None = None,
        log_name: str | None = None,
    ) -> RunResult:
        args = tuple(args)
        self.calls.append((args, Path(log_dir) if log_dir else None, log_name))
        class_name = args[args.index("--strategy") + 1]

        log_path = None
        if log_dir is not None and log_name is not None:
            log_path = Path(log_dir) / log_name
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(f"fake log for {class_name}\n", encoding="utf-8")

        if class_name in self.fail_for:
            return RunResult(
                argv=args, exit_code=1, stdout="boom\n", duration_s=0.0,
                log_path=log_path,
            )
        if args[0] == "hyperopt":
            self._run_hyperopt(class_name)
        elif args[0] == "backtesting":
            self._run_backtest(class_name)
        return RunResult(
            argv=args, exit_code=0, stdout="", duration_s=0.0, log_path=log_path
        )

    def _run_hyperopt(self, class_name: str) -> None:
        if not self.write_sidecar:
            return
        sidecar = self.generated_dir / f"{class_name}.json"
        if class_name in self.sidecar_text:
            self.generated_dir.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(self.sidecar_text[class_name], encoding="utf-8")
            return
        params.write_params_file(
            self.generated_dir / f"{class_name}.py",
            self.sidecar_params.get(class_name, {"buy": {"rsi": 30}}),
            strategy_name=class_name,
        )

    def _run_backtest(self, class_name: str) -> None:
        if not self.write_artifact:
            return
        spec = dict(self.default_zip_spec)
        spec.update(self.zip_specs.get(class_name, {}))
        dest = make_result_zip(self.results_dir, class_name, **spec)
        (self.results_dir / ".last_result.json").write_text(
            json.dumps({"latest_backtest": dest.name}), encoding="utf-8"
        )

    def map_path(self, host_path: Path) -> str:
        """Like DockerRunner.map_path: host path under user_data -> container path."""
        host = Path(host_path)
        try:
            rel = host.resolve().relative_to(self.user_data.resolve())
        except ValueError:
            raise ValueError(f"path {host} is not under user_data {self.user_data}") from None
        if rel == Path("."):
            return CONTAINER_USERDATA
        return f"{CONTAINER_USERDATA}/{rel.as_posix()}"


def _make_ctx(tmp_path: Path, *, config_overrides: dict | None = None, **runner_kwargs):
    """Build a tmp worktree + PipelineContext wired to a FakeRunner."""
    (tmp_path / "config").mkdir()
    cfg = dict(SAMPLE_CONFIG)
    if config_overrides:
        cfg.update(config_overrides)
    (tmp_path / "config" / "miner.json").write_text(json.dumps(cfg, indent=2),
                                                    encoding="utf-8")
    user_data = tmp_path / "freqtrade" / "user_data"
    generated_dir = user_data / "strategies" / "generated"
    results_dir = user_data / "backtest_results"
    generated_dir.mkdir(parents=True)
    results_dir.mkdir(parents=True)
    runner = FakeRunner(
        generated_dir=generated_dir,
        results_dir=results_dir,
        user_data=user_data,
        **runner_kwargs,
    )
    return make_context(tmp_path, runner=runner), runner


def _genome(strategy_id: int) -> StrategyGenome:
    return StrategyGenome(
        strategy_id=strategy_id,
        timeframe="5m",
        can_short=True,
        ema_fast=5,
        ema_slow=20,
        rsi_period=14,
        rsi_long_max=40,
        rsi_short_min=60,
        stoploss=-0.05,
    )


def _seed_candidate(ctx, strategy_id: int) -> int:
    """Write a stub strategy file and register the candidate in the store."""
    genome = _genome(strategy_id)
    py_path = ctx.generated_dir / f"{genome.class_name}.py"
    py_path.write_text(f"# stub strategy {genome.class_name}\n", encoding="utf-8")
    return ctx.store.upsert_candidate(genome, str(py_path.relative_to(ctx.root)))


def _seed_fast_gate_metrics(ctx, candidate_id: int, metrics: dict) -> None:
    """Record a fast_gate run carrying ``metrics`` (as extract_metrics output)."""
    ctx.store.record_run(
        candidate_id, "fast_gate", argv=["backtesting"], exit_code=0,
        duration_s=1.0, metrics=metrics,
    )


class TestHyperoptArgs:
    def test_exact_argv(self, tmp_path: Path) -> None:
        ctx, _runner = _make_ctx(tmp_path)
        assert hyperopt_args(ctx, "Miner_000001", "20220101-20241231", epochs=150) == [
            "hyperopt",
            "--config", "/freqtrade/user_data/config.miner.json",
            "--strategy", "Miner_000001",
            "--strategy-path", "/freqtrade/user_data/strategies/generated",
            "--timerange", "20220101-20241231",
            "--spaces", "buy", "roi", "stoploss",
            "--hyperopt-loss", "MultiMetricHyperOptLoss",
            "-e", "150",
        ]


class TestRunHyperopt:
    def test_success_updates_params_path(self, tmp_path: Path) -> None:
        ctx, runner = _make_ctx(tmp_path)
        cid = _seed_candidate(ctx, 1)

        statuses = run_hyperopt(ctx, [cid], epochs=42)

        assert statuses == {cid: "passed"}
        sidecar = ctx.generated_dir / "Miner_000001.json"
        assert sidecar.is_file()
        row = ctx.store.get_candidate(cid)
        assert row["params_path"] == str(sidecar.relative_to(ctx.root))
        assert ctx.store.stage_status(cid, "hyperopt") == "passed"
        # sidecar round-trips through params.read_params_file
        assert params.read_params_file(sidecar) == {"buy": {"rsi": 30}}
        # the candidate kept its genome + py_path
        assert row["py_path"] == str(
            (ctx.generated_dir / "Miner_000001.py").relative_to(ctx.root)
        )
        assert row["class_name"] == "Miner_000001"

        runs = ctx.store.runs_for(cid, "hyperopt")
        assert len(runs) == 1
        assert runs[0]["exit_code"] == 0
        assert runs[0]["notes"] == "epochs=42"
        assert runs[0]["artifact_path"] == str(sidecar)
        assert runs[0]["argv"][0] == "hyperopt"

        args, log_dir, log_name = runner.calls[0]
        assert list(args) == hyperopt_args(
            ctx, "Miner_000001", ctx.miner_cfg["train_timerange"], epochs=42
        )
        assert log_dir == ctx.root / "agent_logs"
        assert log_name == "hyperopt_Miner_000001.log"

    def test_epochs_defaults_to_300(self, tmp_path: Path) -> None:
        ctx, runner = _make_ctx(tmp_path)
        cid = _seed_candidate(ctx, 1)
        run_hyperopt(ctx, [cid])
        args = runner.calls[0][0]
        assert args[args.index("-e") + 1] == "300"

    def test_epochs_from_config(self, tmp_path: Path) -> None:
        ctx, runner = _make_ctx(tmp_path, config_overrides={"hyperopt": {"epochs": 75}})
        cid = _seed_candidate(ctx, 1)
        run_hyperopt(ctx, [cid])
        args = runner.calls[0][0]
        assert args[args.index("-e") + 1] == "75"

    def test_runner_failure_marks_failed(self, tmp_path: Path) -> None:
        ctx, _runner = _make_ctx(tmp_path, fail_for={"Miner_000002"})
        cid_ok = _seed_candidate(ctx, 1)
        cid_bad = _seed_candidate(ctx, 2)

        statuses = run_hyperopt(ctx, [cid_ok, cid_bad], epochs=5)

        assert statuses[cid_ok] == "passed"
        assert statuses[cid_bad].startswith("failed:")
        assert "exit_code=1" in statuses[cid_bad]
        assert ctx.store.stage_status(cid_bad, "hyperopt") == "failed"
        runs = ctx.store.runs_for(cid_bad, "hyperopt")
        assert len(runs) == 1
        assert runs[0]["exit_code"] == 1
        assert ctx.store.get_candidate(cid_bad)["params_path"] is None

    def test_missing_params_sidecar_fails(self, tmp_path: Path) -> None:
        ctx, _runner = _make_ctx(tmp_path, write_sidecar=False)
        cid = _seed_candidate(ctx, 1)

        statuses = run_hyperopt(ctx, [cid])

        assert statuses[cid].startswith("failed:")
        assert "sidecar" in statuses[cid]
        assert ctx.store.stage_status(cid, "hyperopt") == "failed"
        assert ctx.store.get_candidate(cid)["params_path"] is None
        runs = ctx.store.runs_for(cid, "hyperopt")
        assert len(runs) == 1
        assert runs[0]["exit_code"] == 0

    def test_invalid_params_sidecar_fails(self, tmp_path: Path) -> None:
        ctx, _runner = _make_ctx(
            tmp_path, sidecar_text={"Miner_000001": "not json {"}
        )
        cid = _seed_candidate(ctx, 1)

        statuses = run_hyperopt(ctx, [cid])

        assert statuses[cid].startswith("failed:")
        assert "invalid params sidecar" in statuses[cid]
        assert ctx.store.stage_status(cid, "hyperopt") == "failed"
        assert ctx.store.get_candidate(cid)["params_path"] is None


class TestRunValidation:
    def test_pass_all_gates(self, tmp_path: Path) -> None:
        ctx, runner = _make_ctx(tmp_path)
        cid = _seed_candidate(ctx, 1)

        statuses = run_validation(ctx, [cid])

        assert statuses == {cid: "passed"}
        assert ctx.store.stage_status(cid, "validation") == "passed"

        runs = ctx.store.runs_for(cid, "validation")
        assert len(runs) == 1
        assert runs[0]["exit_code"] == 0
        assert runs[0]["artifact_path"] is not None
        # metrics JSON round-trips through the store
        metrics = runs[0]["metrics"]
        assert metrics["trades"] == 400
        assert metrics["profit_factor"] == 1.5
        assert metrics["max_drawdown_pct"] == 5.0
        assert metrics["pair_coverage"] == 1.0
        assert runs[0]["argv"][0] == "backtesting"

        args, log_dir, log_name = runner.calls[0]
        assert list(args) == backtest_args(
            ctx, "Miner_000001", ctx.miner_cfg["validation_timerange"]
        )
        assert log_dir == ctx.root / "agent_logs"
        assert log_name == "validation_Miner_000001.log"

    def test_hard_filter_rejection(self, tmp_path: Path) -> None:
        # trades=10 < min_trades=300 -> fails the same hard-filter bar as train.
        ctx, _runner = _make_ctx(
            tmp_path, zip_specs={"Miner_000001": {"total_trades": 10}}
        )
        cid = _seed_candidate(ctx, 1)

        statuses = run_validation(ctx, [cid])

        assert statuses[cid].startswith("rejected:")
        assert "hard filters" in statuses[cid]
        assert ctx.store.stage_status(cid, "validation") == "rejected"
        # metrics still recorded even when rejected
        runs = ctx.store.runs_for(cid, "validation")
        assert runs[0]["metrics"]["trades"] == 10

    def test_drawdown_explosion_rejected(self, tmp_path: Path) -> None:
        # validation dd 20% passes the hard filter (<=25%) but exceeds
        # 1.5 * train dd (10%) -> rejected. Train metrics via the arg fallback.
        ctx, _runner = _make_ctx(
            tmp_path, zip_specs={"Miner_000001": {"max_drawdown_account": 0.20}}
        )
        cid = _seed_candidate(ctx, 1)
        train = {cid: {"max_drawdown_pct": 10.0, "profit_factor": 1.0}}

        statuses = run_validation(ctx, [cid], train_metrics=train)

        assert statuses[cid].startswith("rejected:")
        assert "drawdown" in statuses[cid]
        assert ctx.store.stage_status(cid, "validation") == "rejected"

    def test_profit_factor_collapse_rejected(self, tmp_path: Path) -> None:
        # validation pf 1.2 passes the hard filter (>=1.15) but is below
        # 0.6 * train pf (2.5 -> bar 1.5) -> rejected.
        ctx, _runner = _make_ctx(
            tmp_path, zip_specs={"Miner_000001": {"profit_factor": 1.2}}
        )
        cid = _seed_candidate(ctx, 1)
        train = {cid: {"max_drawdown_pct": 5.0, "profit_factor": 2.5}}

        statuses = run_validation(ctx, [cid], train_metrics=train)

        assert statuses[cid].startswith("rejected:")
        assert "profit factor" in statuses[cid]
        assert ctx.store.stage_status(cid, "validation") == "rejected"

    def test_train_metrics_read_from_store(self, tmp_path: Path) -> None:
        # No train_metrics arg: the latest fast_gate run metrics in the store
        # seed the comparison (pf 2.5 -> bar 1.5; validation pf 1.2 collapses).
        ctx, _runner = _make_ctx(
            tmp_path, zip_specs={"Miner_000001": {"profit_factor": 1.2}}
        )
        cid = _seed_candidate(ctx, 1)
        _seed_fast_gate_metrics(
            ctx, cid, {"max_drawdown_pct": 5.0, "profit_factor": 2.5}
        )

        statuses = run_validation(ctx, [cid])

        assert statuses[cid].startswith("rejected:")
        assert "profit factor" in statuses[cid]

    def test_within_bounds_passes(self, tmp_path: Path) -> None:
        # val dd 12% <= 1.5 * 10% and val pf 1.5 >= 0.6 * 2.0 -> passed.
        ctx, _runner = _make_ctx(
            tmp_path, zip_specs={"Miner_000001": {"max_drawdown_account": 0.12}}
        )
        cid = _seed_candidate(ctx, 1)
        train = {cid: {"max_drawdown_pct": 10.0, "profit_factor": 2.0}}

        statuses = run_validation(ctx, [cid], train_metrics=train)

        assert statuses == {cid: "passed"}

    def test_missing_artifact_fails(self, tmp_path: Path) -> None:
        ctx, _runner = _make_ctx(tmp_path, write_artifact=False)
        cid = _seed_candidate(ctx, 1)

        statuses = run_validation(ctx, [cid])

        assert statuses[cid].startswith("failed:")
        assert "artifact" in statuses[cid]
        assert ctx.store.stage_status(cid, "validation") == "failed"

    def test_runner_failure_fails(self, tmp_path: Path) -> None:
        ctx, _runner = _make_ctx(tmp_path, fail_for={"Miner_000001"})
        cid = _seed_candidate(ctx, 1)

        statuses = run_validation(ctx, [cid])

        assert statuses[cid].startswith("failed:")
        assert "exit_code=1" in statuses[cid]
        assert ctx.store.stage_status(cid, "validation") == "failed"
        runs = ctx.store.runs_for(cid, "validation")
        assert runs[0]["exit_code"] == 1


class TestRefinePipeline:
    def test_chains_hyperopt_passed_ids_only(self, tmp_path: Path) -> None:
        ctx, runner = _make_ctx(tmp_path, fail_for={"Miner_000002"})
        ids = [_seed_candidate(ctx, i) for i in (1, 2, 3)]

        result = refine_pipeline(ctx, ids, epochs=10)

        assert result["hyperopt"][ids[0]] == "passed"
        assert result["hyperopt"][ids[1]].startswith("failed:")
        assert result["hyperopt"][ids[2]] == "passed"
        assert set(result["validation"]) == {ids[0], ids[2]}
        assert all(s == "passed" for s in result["validation"].values())

        # 3 hyperopt calls first, then 2 backtesting calls (failed id skipped).
        kinds = [call[0][0] for call in runner.calls]
        assert kinds == ["hyperopt"] * 3 + ["backtesting"] * 2
        validated = {
            call[0][call[0].index("--strategy") + 1]
            for call in runner.calls
            if call[0][0] == "backtesting"
        }
        assert validated == {"Miner_000001", "Miner_000003"}

        # stage statuses visible in the store
        assert ctx.store.stage_status(ids[0], "hyperopt") == "passed"
        assert ctx.store.stage_status(ids[0], "validation") == "passed"
        assert ctx.store.stage_status(ids[1], "hyperopt") == "failed"
        assert ctx.store.stage_status(ids[1], "validation") is None
