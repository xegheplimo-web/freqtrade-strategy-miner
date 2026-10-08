"""Unit tests for the fast-gate stage (no docker, no network).

FakeRunner implements the Runner protocol: on a successful run it copies a
prepared fixture zip (tests/fixtures/backtest-sample.zip) into the results dir
under a chosen name (simulating the freqtrade export), optionally writes
.last_result.json, and returns a RunResult. Every call's args are recorded.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Iterable, Sequence

from strategy_miner.pipeline import backtest_args, make_context
from strategy_miner.pipeline.fast_gate import FastGateResult, run_fast_gate
from strategy_miner.runner import CONTAINER_USERDATA, RunResult

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ZIP = REPO_ROOT / "tests" / "fixtures" / "backtest-sample.zip"
SAMPLE_CONFIG = json.loads((REPO_ROOT / "config" / "miner.json").read_text(encoding="utf-8"))


class FakeRunner:
    """Runner-protocol fake: copies a prepared fixture zip into results_dir on success."""

    def __init__(
        self,
        fixture_zip: Path,
        *,
        results_dir: Path,
        user_data: Path,
        exit_code: int = 0,
        fail_for: Iterable[str] = (),
        copy_artifact: bool = True,
        write_last_result: bool = True,
        fixture_overrides: dict[str, Path] | None = None,
    ) -> None:
        self.fixture_zip = fixture_zip
        self.results_dir = results_dir
        self.user_data = user_data
        self.exit_code = exit_code
        self.fail_for = frozenset(fail_for)
        self.copy_artifact = copy_artifact
        self.write_last_result = write_last_result
        self.fixture_overrides = dict(fixture_overrides or {})
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
        self.calls.append((args, log_dir, log_name))
        class_name = args[args.index("--strategy") + 1]
        exit_code = 1 if class_name in self.fail_for else self.exit_code

        log_path = None
        if log_dir is not None and log_name is not None:
            log_path = Path(log_dir) / log_name
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(f"fake log for {class_name}\n", encoding="utf-8")

        if exit_code == 0 and self.copy_artifact:
            self.results_dir.mkdir(parents=True, exist_ok=True)
            src = self.fixture_overrides.get(class_name, self.fixture_zip)
            dest = self.results_dir / f"backtest-result-{class_name}.zip"
            shutil.copyfile(src, dest)
            if self.write_last_result:
                (self.results_dir / ".last_result.json").write_text(
                    json.dumps({"latest_backtest": dest.name}), encoding="utf-8"
                )
        return RunResult(
            argv=args, exit_code=exit_code, stdout="", duration_s=0.0, log_path=log_path
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


def _make_ctx(
    tmp_path: Path,
    runner: FakeRunner,
    *,
    candidate_count: int = 3,
    selection: dict | None = None,
    config_overrides: dict | None = None,
    remove_keys: Iterable[str] = (),
):
    """Build a tmp worktree + PipelineContext wired to ``runner``."""
    (tmp_path / "config").mkdir()
    cfg = dict(SAMPLE_CONFIG)
    cfg["candidate_count"] = candidate_count
    if selection is not None:
        cfg["selection"] = selection
    if config_overrides:
        cfg.update(config_overrides)
    for key in remove_keys:
        cfg.pop(key, None)
    (tmp_path / "config" / "miner.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    (tmp_path / "freqtrade").mkdir()
    shutil.copyfile(
        REPO_ROOT / "freqtrade" / "config.example.json",
        tmp_path / "freqtrade" / "config.example.json",
    )
    return make_context(tmp_path, runner=runner)


def _make_ctx_with_runner(
    tmp_path: Path,
    *,
    candidate_count: int = 3,
    selection: dict | None = None,
    config_overrides: dict | None = None,
    remove_keys: Iterable[str] = (),
    **runner_kwargs,
) -> tuple:
    """Build a tmp worktree + PipelineContext with a FakeRunner (default: all succeed)."""
    user_data = tmp_path / "freqtrade" / "user_data"
    results_dir = user_data / "backtest_results"
    runner = FakeRunner(FIXTURE_ZIP, results_dir=results_dir, user_data=user_data, **runner_kwargs)
    ctx = _make_ctx(
        tmp_path,
        runner,
        candidate_count=candidate_count,
        selection=selection,
        config_overrides=config_overrides,
        remove_keys=remove_keys,
    )
    return ctx, runner


def _make_bad_fixture(tmp_path: Path) -> Path:
    """A fixture zip whose metrics fail the hard filters (too few trades, low PF)."""
    with zipfile.ZipFile(FIXTURE_ZIP) as zf:
        name = next(
            n for n in zf.namelist() if n.endswith(".json") and not n.endswith("_config.json")
        )
        summary = json.loads(zf.read(name))["strategy"]["ZarTest02SL35"]
    summary["total_trades"] = 10
    summary["profit_factor"] = 1.0
    summary["expectancy"] = -1.0
    summary["max_drawdown_account"] = 0.9
    payload = {"strategy": {"ZarTest02SL35": summary}, "strategy_comparison": []}
    path = tmp_path / "bad-fixture.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("backtest-result-bad.json", json.dumps(payload))
    return path


class TestHappyPath:
    def test_all_survive_and_shortlist(self, tmp_path: Path) -> None:
        # k = max(ceil(0.5 * 3), 1) = 2 -> top 2 of 3 survivors shortlisted.
        ctx, runner = _make_ctx_with_runner(
            tmp_path, selection={"top_fraction": 0.5, "min_candidates": 1}
        )
        result = run_fast_gate(ctx)

        assert isinstance(result, FastGateResult)
        assert result.generated == 3
        assert result.backtested == 3
        assert result.failed_runs == 0
        assert result.rejected == 0
        assert len(result.shortlisted) == 2
        assert isinstance(result.shortlisted, tuple)
        # scores/artifact_paths cover ALL survivors (3), not only the shortlisted.
        assert len(result.scores) == 3
        assert len(result.artifact_paths) == 3
        assert all(isinstance(v, str) for v in result.artifact_paths.values())
        # same fixture for all -> identical scores.
        assert len(set(result.scores.values())) == 1

        # Store statuses: 2 shortlisted + 1 rejected (passed filters, outside top k).
        by_name = {c["class_name"]: c["id"] for c in ctx.store.list_candidates()}
        assert len(by_name) == 3
        statuses = {
            name: ctx.store.stage_status(cid, "fast_gate") for name, cid in by_name.items()
        }
        assert sum(1 for s in statuses.values() if s == "shortlisted") == 2
        assert sum(1 for s in statuses.values() if s == "rejected") == 1
        assert set(result.shortlisted) <= set(by_name.values())

        # Runner called once per candidate with the EXACT backtest args.
        assert len(runner.calls) == 3
        for args, log_dir, log_name in runner.calls:
            class_name = args[args.index("--strategy") + 1]
            assert tuple(args) == tuple(
                backtest_args(ctx, class_name, ctx.miner_cfg["train_timerange"])
            )
            assert log_dir == ctx.root / "agent_logs"
            assert log_name == f"fast_gate_{class_name}.log"

        # Metrics recorded in the store runs for every candidate.
        for cid in by_name.values():
            runs = ctx.store.runs_for(cid, "fast_gate")
            assert len(runs) == 1
            assert runs[0]["exit_code"] == 0
            assert runs[0]["metrics"]["trades"] == 4705
            assert runs[0]["artifact_path"] is not None

        # Strategy files written + candidates upserted with root-relative py_path.
        for class_name, cid in by_name.items():
            assert (ctx.generated_dir / f"{class_name}.py").is_file()
            row = ctx.store.get_candidate(cid)
            expected = str((ctx.generated_dir / f"{class_name}.py").relative_to(ctx.root))
            assert row["py_path"] == expected

    def test_fewer_survivors_than_k_takes_all(self, tmp_path: Path) -> None:
        # Default selection: k = max(ceil(0.05 * 3), 12) = 12 > 3 survivors -> all shortlisted.
        ctx, _runner = _make_ctx_with_runner(tmp_path)
        result = run_fast_gate(ctx)
        assert result.generated == 3
        assert result.backtested == 3
        assert len(result.shortlisted) == 3
        assert result.rejected == 0
        assert ctx.store.shortlist() == sorted(result.shortlisted)

    def test_count_overrides_candidate_count(self, tmp_path: Path) -> None:
        ctx, _runner = _make_ctx_with_runner(tmp_path, candidate_count=5)
        result = run_fast_gate(ctx, count=2)
        assert result.generated == 2
        assert result.backtested == 2
        assert len(ctx.store.list_candidates()) == 2


class TestFailurePath:
    def test_one_candidate_fails(self, tmp_path: Path) -> None:
        ctx, _runner = _make_ctx_with_runner(
            tmp_path,
            selection={"top_fraction": 0.5, "min_candidates": 1},
            fail_for={"Miner_000002"},
        )
        result = run_fast_gate(ctx)

        assert result.generated == 3
        assert result.backtested == 2
        assert result.failed_runs == 1

        by_name = {c["class_name"]: c["id"] for c in ctx.store.list_candidates()}
        failed_id = by_name["Miner_000002"]
        assert ctx.store.stage_status(failed_id, "fast_gate") == "failed"
        # Failed run recorded with exit_code 1 and a note.
        runs = ctx.store.runs_for(failed_id, "fast_gate")
        assert len(runs) == 1
        assert runs[0]["exit_code"] == 1
        assert "exit_code=1" in (runs[0]["notes"] or "")
        assert failed_id not in result.artifact_paths
        assert failed_id not in result.scores

        # Others unaffected: 2 survivors, k = max(ceil(0.5 * 3), 1) = 2 -> both shortlisted.
        assert set(result.shortlisted) == {by_name["Miner_000001"], by_name["Miner_000003"]}
        assert ctx.store.stage_status(by_name["Miner_000001"], "fast_gate") == "shortlisted"
        assert ctx.store.stage_status(by_name["Miner_000003"], "fast_gate") == "shortlisted"


class TestArtifactMissing:
    def test_success_without_artifact_counts_as_failed(self, tmp_path: Path) -> None:
        ctx, _runner = _make_ctx_with_runner(tmp_path, candidate_count=2, copy_artifact=False)
        result = run_fast_gate(ctx)

        assert result.generated == 2
        assert result.backtested == 0
        assert result.failed_runs == 2
        assert result.shortlisted == ()
        assert result.scores == {}
        assert result.artifact_paths == {}

        for row in ctx.store.list_candidates():
            assert ctx.store.stage_status(row["id"], "fast_gate") == "failed"
            runs = ctx.store.runs_for(row["id"], "fast_gate")
            assert len(runs) == 1
            assert runs[0]["exit_code"] == 0
            assert "artifact" in (runs[0]["notes"] or "")


class TestHardFilterRejection:
    def test_candidate_failing_filters_rejected(self, tmp_path: Path) -> None:
        bad_zip = _make_bad_fixture(tmp_path)
        ctx, _runner = _make_ctx_with_runner(
            tmp_path,
            selection={"top_fraction": 0.5, "min_candidates": 1},
            fixture_overrides={"Miner_000002": bad_zip},
        )
        result = run_fast_gate(ctx)

        assert result.generated == 3
        assert result.backtested == 3
        assert result.failed_runs == 0
        assert result.rejected == 1

        by_name = {c["class_name"]: c["id"] for c in ctx.store.list_candidates()}
        rejected_id = by_name["Miner_000002"]
        assert ctx.store.stage_status(rejected_id, "fast_gate") == "rejected"
        assert rejected_id not in result.scores
        assert rejected_id not in result.artifact_paths
        # scores cover only the 2 survivors.
        assert len(result.scores) == 2
        # k = max(ceil(0.5 * 3), 1) = 2 -> both survivors shortlisted.
        assert set(result.shortlisted) == {by_name["Miner_000001"], by_name["Miner_000003"]}


class TestScreenFilters:
    """The pre-hyperopt screen (``fast_gate_filters``) vs the hard_filters fallback."""

    def test_screen_filters_take_precedence(self, tmp_path: Path) -> None:
        # The bad fixture fails the production hard_filters (10 trades, dd 90%)
        # but passes a deliberately loose fast_gate_filters screen -> it must NOT
        # be rejected, proving the screen key takes precedence at this stage.
        bad_zip = _make_bad_fixture(tmp_path)
        ctx, _runner = _make_ctx_with_runner(
            tmp_path,
            config_overrides={
                "fast_gate_filters": {
                    "min_trades": 5,
                    "max_drawdown_pct": 95.0,
                    "min_profit_factor": 0.0,
                    "min_pair_coverage": 0.0,
                    "min_expectancy": None,
                }
            },
            fixture_overrides={"Miner_000002": bad_zip},
        )
        result = run_fast_gate(ctx)

        assert result.backtested == 3
        assert result.rejected == 0
        assert len(result.scores) == 3

    def test_falls_back_to_hard_filters_without_screen_key(self, tmp_path: Path) -> None:
        # Without fast_gate_filters the stage keeps using hard_filters (the
        # pre-D-017 behavior, still exercised by the acceptance configs).
        bad_zip = _make_bad_fixture(tmp_path)
        ctx, _runner = _make_ctx_with_runner(
            tmp_path,
            remove_keys=("fast_gate_filters",),
            fixture_overrides={"Miner_000002": bad_zip},
        )
        result = run_fast_gate(ctx)

        assert result.backtested == 3
        assert result.rejected == 1
        assert len(result.scores) == 2
