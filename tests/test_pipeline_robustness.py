"""Unit tests for the robustness stage (no docker, no network).

FakeRunner implements the Runner protocol: on each run it copies a prepared
synthetic backtest zip into the results dir (simulating the freqtrade export),
writes .last_result.json, and returns a RunResult. Fee runs (``--fee`` in argv)
and perturb variants (``<Class>_p<k>`` strategy names) can be served different
fixtures so each check's pass/fail matrix is controllable.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Sequence

from strategy_miner.generator import GenomeGenerator
from strategy_miner.params import read_params_file, write_params_file
from strategy_miner.pipeline import make_context
from strategy_miner.pipeline.robustness import (
    ROBUSTNESS_CONTAINER,
    RobustnessResult,
    _robustness_backtest_args,
    run_robustness,
)
from strategy_miner.runner import CONTAINER_USERDATA, RunResult

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_CONFIG = json.loads((REPO_ROOT / "config" / "miner.json").read_text(encoding="utf-8"))

ORIG_PARAMS = {
    "buy_rsi": 30,
    "buy_ema_fast": 12,
    "sell_factor": 1.5,
    "stoploss": -0.08,
    "use_flag": True,
    "label": "unchanged",
    "nothing": None,
    "nested": {"window": 100, "ratio": 2.5},
}


class FakeRunner:
    """Runner-protocol fake serving per-strategy synthetic artifacts."""

    def __init__(
        self,
        *,
        results_dir: Path,
        user_data: Path,
        default_zip: Path,
        fee_zip: Path | None = None,
        perturb_zips: dict[str, Path] | None = None,
        fail_for: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        self.results_dir = results_dir
        self.user_data = user_data
        self.default_zip = default_zip
        self.fee_zip = fee_zip or default_zip
        self.perturb_zips = dict(perturb_zips or {})
        self.fail_for = frozenset(fail_for)
        self.calls: list[tuple[str, ...]] = []
        self._counter = 0

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
        if log_dir is not None and log_name is not None:
            log_path = Path(log_dir) / log_name
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(f"fake log for {class_name}\n", encoding="utf-8")
        else:
            log_path = None
        if class_name in self.fail_for:
            return RunResult(argv=args, exit_code=1, stdout="", duration_s=0.0,
                             log_path=log_path)
        if "--fee" in args:
            src = self.fee_zip
        else:
            src = self.perturb_zips.get(class_name, self.default_zip)
        self._counter += 1
        self.results_dir.mkdir(parents=True, exist_ok=True)
        dest = self.results_dir / f"backtest-result-{class_name}-{self._counter}.zip"
        shutil.copyfile(src, dest)
        (self.results_dir / ".last_result.json").write_text(
            json.dumps({"latest_backtest": dest.name}), encoding="utf-8"
        )
        return RunResult(argv=args, exit_code=0, stdout="", duration_s=0.0,
                         log_path=log_path)

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


def _make_backtest_zip(
    path: Path, strategy_name: str, profit_total: float,
    profit_factor: float, profits: list[float],
) -> Path:
    """Write a synthetic backtest-result zip with known summary + trades."""
    summary = {
        "strategy_name": strategy_name,
        "profit_total": profit_total,
        "profit_factor": profit_factor,
        "total_trades": len(profits),
        "trades": [{"profit_abs": v} for v in profits],
        "results_per_pair": [{"key": "BTC/USDT:USDT", "trades": len(profits)}],
        "pairlist": ["BTC/USDT:USDT"],
        "periodic_breakdown": {"month": [{"profit_abs": sum(profits)}]},
    }
    payload = {"strategy": {strategy_name: summary}, "strategy_comparison": []}
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("backtest-result-synth.json", json.dumps(payload))
    return path


def _make_ctx(tmp_path: Path, runner, *, robustness: dict | None = None):
    """Build a tmp worktree + PipelineContext wired to ``runner``."""
    (tmp_path / "config").mkdir(exist_ok=True)
    cfg = dict(SAMPLE_CONFIG)
    if robustness is not None:
        cfg["robustness"] = robustness
    (tmp_path / "config" / "miner.json").write_text(json.dumps(cfg, indent=2),
                                                    encoding="utf-8")
    (tmp_path / "freqtrade").mkdir(exist_ok=True)
    shutil.copyfile(
        REPO_ROOT / "freqtrade" / "config.example.json",
        tmp_path / "freqtrade" / "config.example.json",
    )
    return make_context(tmp_path, runner=runner)


def _register_candidate(ctx, class_seed: int = 1,
                        params: dict | None = None) -> tuple[int, str]:
    """Upsert a candidate with a real strategy file + params sidecar."""
    genome = GenomeGenerator(seed=class_seed).generate_one(class_seed)
    class_name = genome.class_name
    py_path = ctx.generated_dir / f"{class_name}.py"
    from strategy_miner.compiler import write_strategy
    write_strategy(genome, ctx.generated_dir)
    assert py_path.is_file()
    write_params_file(py_path, dict(ORIG_PARAMS if params is None else params),
                      strategy_name=class_name)
    candidate_id = ctx.store.upsert_candidate(
        genome, str(py_path.relative_to(ctx.root))
    )
    return candidate_id, class_name


def _record_validation(ctx, candidate_id: int, zip_path: Path) -> None:
    ctx.store.record_run(candidate_id, "validation", argv=["backtesting"],
                         exit_code=0, duration_s=0.0, artifact_path=str(zip_path))


def _setup(
    tmp_path: Path, *, robustness: dict | None = None,
    fee: tuple[float, float] = (0.5, 1.2),
    perturb_profits: tuple[float, float] = (0.5, 1.5),
    validation_profits: list[float] | None = None,
    perturb_zips: dict[str, Path] | None = None,
    class_seed: int = 1,
):
    """Full fixture: ctx + runner + candidate + validation artifact.

    Returns (ctx, runner, candidate_id, class_name).
    """
    user_data = tmp_path / "freqtrade" / "user_data"
    results_dir = user_data / "backtest_results"
    default_zip = _make_backtest_zip(
        tmp_path / "default.zip", "PLACEHOLDER", *perturb_profits,
        profits=[10.0, 8.0, -2.0, 5.0, 4.0, 3.0, -1.0, 2.0],
    )
    fee_zip = _make_backtest_zip(
        tmp_path / "fee.zip", "PLACEHOLDER", *fee,
        profits=[10.0, 8.0, -2.0, 5.0, 4.0, 3.0, -1.0, 2.0],
    )
    runner = FakeRunner(results_dir=results_dir, user_data=user_data,
                        default_zip=default_zip, fee_zip=fee_zip,
                        perturb_zips=perturb_zips)
    ctx = _make_ctx(tmp_path, runner, robustness=robustness)
    candidate_id, class_name = _register_candidate(ctx, class_seed=class_seed)
    # Fix placeholder strategy names inside fixture zips to the real class name.
    for zpath in (default_zip, fee_zip):
        with zipfile.ZipFile(zpath) as zf:
            payload = json.loads(zf.read("backtest-result-synth.json"))
        summary = payload["strategy"].pop("PLACEHOLDER")
        payload["strategy"][class_name] = summary
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("backtest-result-synth.json", json.dumps(payload))
    if validation_profits is None:
        validation_profits = [10.0] * 20
    val_zip = _make_backtest_zip(tmp_path / "validation.zip", class_name, 1.0, 2.0,
                                 validation_profits)
    dest = results_dir / "backtest-result-validation.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(val_zip, dest)
    _record_validation(ctx, candidate_id, dest)
    return ctx, runner, candidate_id, class_name


class TestRobustnessBacktestArgs:
    def test_exact_list_with_container_path(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, runner=object())
        args = _robustness_backtest_args(ctx, "Miner_000001_p0", "20250101-20251231",
                                         ROBUSTNESS_CONTAINER)
        assert args == [
            "backtesting",
            "--config", f"{CONTAINER_USERDATA}/config.miner.json",
            "--strategy", "Miner_000001_p0",
            "--strategy-path", ROBUSTNESS_CONTAINER,
            "--timerange", "20250101-20251231",
            "--export", "trades",
        ]

    def test_host_path_is_mapped(self, tmp_path: Path) -> None:
        user_data = tmp_path / "freqtrade" / "user_data"
        runner = FakeRunner(results_dir=user_data / "backtest_results",
                            user_data=user_data,
                            default_zip=tmp_path / "x.zip")
        ctx = _make_ctx(tmp_path, runner=runner)
        host_dir = ctx.user_data / "strategies" / "robustness"
        args = _robustness_backtest_args(ctx, "Miner_000001_p1", "20250101-20251231",
                                         host_dir)
        assert args[args.index("--strategy-path") + 1] == ROBUSTNESS_CONTAINER


class TestFeeCheck:
    def test_fee_pass(self, tmp_path: Path) -> None:
        ctx, _runner, cid, _name = _setup(tmp_path, fee=(0.5, 1.2))
        assert run_robustness(ctx, [cid]) == {cid: "passed"}

    def test_fee_fail_low_profit_factor(self, tmp_path: Path) -> None:
        ctx, _runner, cid, _name = _setup(tmp_path, fee=(0.5, 0.95))
        assert run_robustness(ctx, [cid]) == {cid: "rejected: fee"}

    def test_fee_fail_negative_profit(self, tmp_path: Path) -> None:
        ctx, _runner, cid, _name = _setup(tmp_path, fee=(-0.1, 2.0))
        assert run_robustness(ctx, [cid]) == {cid: "rejected: fee"}

    def test_fee_uses_stressed_fee_flag(self, tmp_path: Path) -> None:
        ctx, runner, cid, _name = _setup(
            tmp_path, robustness={"base_fee": 0.001, "fee_multiplier": 2.0})
        run_robustness(ctx, [cid])
        fee_calls = [c for c in runner.calls if "--fee" in c]
        assert len(fee_calls) == 1
        assert fee_calls[0][fee_calls[0].index("--fee") + 1] == str(0.001 * 2.0)


class TestPerturb:
    def test_files_created_and_within_pct(self, tmp_path: Path) -> None:
        ctx, _runner, cid, class_name = _setup(tmp_path)
        run_robustness(ctx, [cid])
        rob_dir = ctx.user_data / "strategies" / "robustness"
        pct = 0.07
        for k in range(3):
            py_file = rob_dir / f"{class_name}_p{k}.py"
            assert py_file.is_file()
            assert f"class {class_name}_p{k}" in py_file.read_text(encoding="utf-8")
            sidecar = py_file.with_suffix(".json")
            assert sidecar.is_file()
            raw = json.loads(sidecar.read_text(encoding="utf-8"))
            # freqtrade validates this field against the loaded class name.
            assert raw["strategy_name"] == f"{class_name}_p{k}"
            perturbed = read_params_file(sidecar)
            assert perturbed["use_flag"] is True
            assert perturbed["label"] == "unchanged"
            assert perturbed["nothing"] is None
            for old, new in ((30, perturbed["buy_rsi"]),
                             (12, perturbed["buy_ema_fast"])):
                assert abs(new - old) / old <= pct + 1.0 / old + 1e-9
            for old, new in ((1.5, perturbed["sell_factor"]),
                             (-0.08, perturbed["stoploss"])):
                assert abs(new - old) / abs(old) <= pct + 1e-3
            assert abs(perturbed["nested"]["window"] - 100) / 100 <= pct + 0.01 + 1e-9

    def test_seeded_determinism(self, tmp_path: Path) -> None:
        ctx, _runner, cid, class_name = _setup(tmp_path)
        run_robustness(ctx, [cid])
        rob_dir = ctx.user_data / "strategies" / "robustness"
        first_py = {p.name: p.read_bytes() for p in sorted(rob_dir.glob("*.py"))}
        first_params = {
            p.name: read_params_file(p) for p in sorted(rob_dir.glob("*.json"))
        }
        assert len(first_py) == 3 and len(first_params) == 3
        run_robustness(ctx, [cid])
        second_py = {p.name: p.read_bytes() for p in sorted(rob_dir.glob("*.py"))}
        second_params = {
            p.name: read_params_file(p) for p in sorted(rob_dir.glob("*.json"))
        }
        # Same seed -> same strategy text and same perturbed params
        # (sidecar export_time timestamps are excluded from the comparison).
        assert first_py == second_py
        assert first_params == second_params

    def test_one_of_three_fails_check(self, tmp_path: Path) -> None:
        user_data = tmp_path / "freqtrade" / "user_data"
        results_dir = user_data / "backtest_results"
        good = _make_backtest_zip(tmp_path / "good.zip", "V", 0.5, 1.5,
                                  [10.0, 8.0, -2.0, 5.0])
        bad = _make_backtest_zip(tmp_path / "bad.zip", "V", -0.5, 0.4,
                                 [-10.0, -8.0, 2.0, -5.0])
        runner = FakeRunner(results_dir=results_dir, user_data=user_data,
                            default_zip=good, fee_zip=good)
        ctx = _make_ctx(tmp_path, runner=runner)
        cid, class_name = _register_candidate(ctx)
        runner.perturb_zips = {
            f"{class_name}_p0": good,
            f"{class_name}_p1": bad,
            f"{class_name}_p2": bad,
        }
        for zpath in (good, bad):
            with zipfile.ZipFile(zpath) as zf:
                payload = json.loads(zf.read("backtest-result-synth.json"))
            summary = payload["strategy"].pop("V")
            payload["strategy"][f"{class_name}_p0"] = summary
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("backtest-result-synth.json", json.dumps(payload))
        val = _make_backtest_zip(tmp_path / "v.zip", class_name, 1.0, 2.0,
                                 [10.0] * 20)
        dest = results_dir / "backtest-result-validation.zip"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(val, dest)
        _record_validation(ctx, cid, dest)
        assert run_robustness(ctx, [cid]) == {cid: "rejected: perturb"}

    def test_two_of_three_passes_check(self, tmp_path: Path) -> None:
        user_data = tmp_path / "freqtrade" / "user_data"
        results_dir = user_data / "backtest_results"
        variants: dict[str, Path] = {}
        for k, ok in enumerate((True, True, False)):
            profit, pf = (0.5, 1.5) if ok else (-0.5, 0.4)
            zpath = _make_backtest_zip(tmp_path / f"pz{k}.zip", "V", profit, pf,
                                       [10.0, 8.0, -2.0, 5.0])
            variants[f"p{k}"] = (zpath, profit, pf)
        runner = FakeRunner(results_dir=results_dir, user_data=user_data,
                            default_zip=tmp_path / "pz0.zip")
        ctx = _make_ctx(tmp_path, runner=runner)
        cid, class_name = _register_candidate(ctx)
        fee_zip = _make_backtest_zip(tmp_path / "fee.zip", class_name, 0.5, 1.2,
                                     [10.0, 8.0, -2.0, 5.0])
        runner.fee_zip = fee_zip
        runner.default_zip = tmp_path / "pz0.zip"
        perturb_zips = {}
        for k in range(3):
            zpath, _profit, _pf = variants[f"p{k}"]
            with zipfile.ZipFile(zpath) as zf:
                payload = json.loads(zf.read("backtest-result-synth.json"))
            summary = payload["strategy"].pop("V")
            payload["strategy"][f"{class_name}_p{k}"] = summary
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("backtest-result-synth.json", json.dumps(payload))
            perturb_zips[f"{class_name}_p{k}"] = zpath
        runner.perturb_zips = perturb_zips
        val = _make_backtest_zip(tmp_path / "v.zip", class_name, 1.0, 2.0,
                                 [10.0] * 20)
        dest = results_dir / "backtest-result-validation.zip"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(val, dest)
        _record_validation(ctx, cid, dest)
        assert run_robustness(ctx, [cid]) == {cid: "passed"}

    def test_threshold_uses_actual_run_count(self, tmp_path: Path) -> None:
        # perturb_runs=2 -> required = ceil(0.66*2) = 2, so a single pass fails.
        ctx, runner, cid, class_name = _setup(
            tmp_path, robustness={"perturb_runs": 2},
            perturb_profits=(0.5, 1.5))
        assert run_robustness(ctx, [cid]) == {cid: "passed"}
        detail = json.loads(
            (ctx.root / "output" / "robustness" / f"{class_name}.json")
            .read_text(encoding="utf-8"))
        assert detail["perturb"]["runs"] == 2
        assert detail["perturb"]["required"] == 2


class TestDropTop:
    def test_concentrated_profit_fails(self, tmp_path: Path) -> None:
        profits = [50.0] * 5 + [-1.0] * 15
        ctx, _runner, cid, class_name = _setup(tmp_path, validation_profits=profits)
        assert run_robustness(ctx, [cid]) == {cid: "rejected: drop_top"}
        detail = json.loads(
            (ctx.root / "output" / "robustness" / f"{class_name}.json")
            .read_text(encoding="utf-8"))
        assert detail["drop_top"]["n"] == 5
        assert detail["drop_top"]["trades"] == 20
        assert detail["drop_top"]["profit_abs_after"] < 0

    def test_dispersed_profit_passes(self, tmp_path: Path) -> None:
        ctx, _runner, cid, class_name = _setup(
            tmp_path, validation_profits=[10.0] * 20)
        assert run_robustness(ctx, [cid]) == {cid: "passed"}
        detail = json.loads(
            (ctx.root / "output" / "robustness" / f"{class_name}.json")
            .read_text(encoding="utf-8"))
        assert detail["drop_top"]["n"] == 5
        assert detail["drop_top"]["profit_abs_after"] == 150.0

    def test_missing_validation_artifact_fails(self, tmp_path: Path) -> None:
        user_data = tmp_path / "freqtrade" / "user_data"
        results_dir = user_data / "backtest_results"
        good = _make_backtest_zip(tmp_path / "good.zip", "V", 0.5, 1.5,
                                  [10.0, 8.0, -2.0, 5.0])
        runner = FakeRunner(results_dir=results_dir, user_data=user_data,
                            default_zip=good, fee_zip=good)
        ctx = _make_ctx(tmp_path, runner=runner)
        cid, class_name = _register_candidate(ctx)
        assert run_robustness(ctx, [cid]) == {cid: "rejected: drop_top"}
        detail = json.loads(
            (ctx.root / "output" / "robustness" / f"{class_name}.json")
            .read_text(encoding="utf-8"))
        assert detail["drop_top"]["note"] == "no validation artifact"


class TestOverall:
    def test_statuses_runs_and_detail_file(self, tmp_path: Path) -> None:
        ctx, runner, cid, class_name = _setup(tmp_path)
        statuses = run_robustness(ctx, [cid])
        assert statuses == {cid: "passed"}
        assert ctx.store.stage_status(cid, "robustness") == "passed"

        runs = ctx.store.runs_for(cid, "robustness")
        assert len(runs) == 1 + 3  # fee + perturb_runs
        assert runs[0]["notes"] == "check=fee"
        for k in range(3):
            assert runs[1 + k]["notes"] == f"check=perturb k={k} seed={cid * 1000 + k}"
        assert all(r["exit_code"] == 0 for r in runs)

        fee_calls = [c for c in runner.calls if "--fee" in c]
        assert len(fee_calls) == 1
        assert fee_calls[0][fee_calls[0].index("--strategy") + 1] == class_name
        perturb_calls = [c for c in runner.calls if "--fee" not in c]
        assert len(perturb_calls) == 3
        for c in perturb_calls:
            assert c[c.index("--strategy-path") + 1] == ROBUSTNESS_CONTAINER

        detail_path = ctx.root / "output" / "robustness" / f"{class_name}.json"
        assert detail_path.is_file()
        detail = json.loads(detail_path.read_text(encoding="utf-8"))
        assert detail["status"] == "passed"
        assert detail["perturb"]["passed_runs"] == 3
        assert detail["perturb"]["runs"] == 3
        assert detail["fee"]["passed"] is True
        assert detail["drop_top"]["passed"] is True

    def test_multiple_candidates_and_rejection_reasons(self, tmp_path: Path) -> None:
        ctx, runner, cid1, _name = _setup(tmp_path, fee=(0.5, 1.2), class_seed=1)
        cid2, _name2 = _register_candidate(ctx, class_seed=2)
        statuses = run_robustness(ctx, [cid1, cid2])
        # cid2 has no validation artifact -> drop_top fails.
        assert statuses == {cid1: "passed", cid2: "rejected: drop_top"}
        assert ctx.store.stage_status(cid2, "robustness") == "rejected: drop_top"

    def test_result_dataclass_shape(self, tmp_path: Path) -> None:
        result = RobustnessResult(candidate_id=7, class_name="Demo",
                                  fee={"passed": True},
                                  perturb={"passed_runs": 3, "runs": 3},
                                  drop_top={"passed": True}, status="passed")
        assert result.to_dict()["status"] == "passed"
