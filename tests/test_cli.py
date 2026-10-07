"""Unit tests for the CLI wiring (no docker, no network).

A fake context factory (FakeRunner + tmp Store + tmp tree with config) is
injected via ``cli.main(..., ctx_factory=...)``; the stage functions are
monkeypatched in the ``cli`` namespace.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from strategy_miner import cli  # noqa: E402
from strategy_miner.genome import StrategyGenome  # noqa: E402
from strategy_miner.pipeline import make_context  # noqa: E402
from strategy_miner.pipeline.fast_gate import FastGateResult  # noqa: E402
from strategy_miner.runner import RunResult  # noqa: E402

MINER_CFG = {
    "seed": 7,
    "candidate_count": 4,
    "timeframe": "5m",
    "can_short": False,
    "train_timerange": "20220101-20221231",
    "validation_timerange": "20230101-20230331",
    "final_test_timerange": "20230401-20230630",
    "exchange": "binance",
    "trading_mode": "futures",
    "pairs": ["BTC/USDT:USDT"],
    "hard_filters": {
        "min_trades": 1,
        "max_drawdown_pct": 99.0,
        "min_profit_factor": 0.1,
        "min_pair_coverage": 0.0,
    },
    "selection": {"top_fraction": 0.5, "min_candidates": 1},
}

COMMANDS = [
    "generate",
    "data-download",
    "data-audit",
    "fast-gate",
    "hyperopt",
    "validate",
    "bias",
    "walk-forward",
    "robust",
    "final",
    "report",
    "status",
    "run",
]

TRAIN_METRICS = {
    "trades": 10,
    "profit_factor": 1.5,
    "total_return_pct": 12.0,
    "max_drawdown_pct": 5.0,
}


class FakeRunner:
    """Runner-protocol fake: records calls, never touches docker."""

    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.calls: list[tuple[tuple[str, ...], Path | None, str | None]] = []

    def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: int = 3600,
        log_dir: Path | None = None,
        log_name: str | None = None,
    ) -> RunResult:
        self.calls.append((tuple(args), log_dir, log_name))
        log_path = None
        if log_dir is not None and log_name is not None:
            log_path = Path(log_dir) / log_name
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("fake log\n", encoding="utf-8")
        return RunResult(
            argv=tuple(args),
            exit_code=self.exit_code,
            stdout="fake stdout",
            duration_s=0.01,
            log_path=log_path,
        )

    def map_path(self, host_path: Path) -> str:
        return f"/freqtrade/user_data/{Path(host_path).name}"


def _genome(strategy_id: int) -> StrategyGenome:
    return StrategyGenome(
        strategy_id=strategy_id,
        timeframe="5m",
        can_short=False,
        ema_fast=10,
        ema_slow=30,
        rsi_period=14,
        rsi_long_max=30,
        rsi_short_min=70,
        stoploss=-0.1,
    )


@pytest.fixture
def fake_env(tmp_path: Path) -> SimpleNamespace:
    """Tmp tree with config + cached fake-context factory sharing one store."""
    root = tmp_path / "ws"
    (root / "config").mkdir(parents=True)
    (root / "config" / "miner.json").write_text(
        json.dumps(MINER_CFG), encoding="utf-8"
    )
    (root / "freqtrade").mkdir(parents=True, exist_ok=True)
    (root / "freqtrade" / "config.example.json").write_text("{}", encoding="utf-8")
    runner = FakeRunner()
    cache: dict[str, object] = {}

    def factory(r: Path | str, *, config_path: Path | str | None = None,
                **kwargs: object) -> object:
        key = str(r)
        if key not in cache:
            cache[key] = make_context(r, config_path=config_path, runner=runner)
        return cache[key]

    return SimpleNamespace(root=root, runner=runner, factory=factory)


def _seed(env: SimpleNamespace, strategy_id: int,
          statuses: dict[str, str] | None = None,
          metrics: dict | None = None) -> int:
    ctx = env.factory(env.root, config_path=env.root / "config" / "miner.json")
    candidate_id = ctx.store.upsert_candidate(
        _genome(strategy_id), f"gen/Miner_{strategy_id:06d}.py"
    )
    for stage, status in (statuses or {}).items():
        ctx.store.set_stage_status(candidate_id, stage, status)
    if metrics is not None:
        ctx.store.record_run(
            candidate_id, "fast_gate", ["backtesting"], 0, 0.1,
            artifact_path="results/x.zip", metrics=metrics,
        )
    return candidate_id


def _ctx(env: SimpleNamespace) -> object:
    return env.factory(env.root, config_path=env.root / "config" / "miner.json")


# --------------------------------------------------------------------------- parser


@pytest.mark.parametrize("command", COMMANDS)
def test_each_command_parses(command: str) -> None:
    args = cli.build_parser().parse_args([command])
    assert args.command == command


def test_parser_defaults() -> None:
    args = cli.build_parser().parse_args(["status"])
    assert args.root == cli.REPO_ROOT
    assert args.config is None  # main() resolves it to <root>/config/miner.json
    assert cli.build_parser().parse_args(["fast-gate"]).count is None
    assert cli.build_parser().parse_args(["hyperopt"]).epochs is None
    assert cli.build_parser().parse_args(["walk-forward"]).max_folds is None
    assert cli.build_parser().parse_args(["final"]).force is False
    assert cli.build_parser().parse_args(["data-download"]).timerange is None


def test_parser_ids_parse_ints() -> None:
    args = cli.build_parser().parse_args(["hyperopt", "--ids", "1", "2", "--epochs", "5"])
    assert args.ids == [1, 2]
    assert args.epochs == 5
    assert cli.build_parser().parse_args(["validate", "--ids", "3"]).ids == [3]
    assert cli.build_parser().parse_args(["walk-forward", "--max-folds", "3"]).max_folds == 3
    assert cli.build_parser().parse_args(["final", "--force"]).force is True


# --------------------------------------------------------------------------- dispatch


def test_fast_gate_dispatch_passes_count(
    fake_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    seen: dict[str, object] = {}

    def fake_run_fast_gate(ctx: object, *, count: int | None = None) -> FastGateResult:
        seen["count"] = count
        return FastGateResult(
            generated=2, backtested=2, failed_runs=0, rejected=1,
            shortlisted=(1,), scores={1: 1.5}, artifact_paths={1: "a.zip"},
        )

    monkeypatch.setattr(cli, "run_fast_gate", fake_run_fast_gate)
    code = cli.main(
        ["--root", str(fake_env.root), "fast-gate", "--count", "2"],
        ctx_factory=fake_env.factory,
    )
    assert code == 0
    assert seen["count"] == 2
    assert json.loads(capsys.readouterr().out)["generated"] == 2


def test_fast_gate_failed_runs_exit_1(
    fake_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run_fast_gate(ctx: object, *, count: int | None = None) -> FastGateResult:
        return FastGateResult(
            generated=1, backtested=0, failed_runs=1, rejected=0,
            shortlisted=(), scores={}, artifact_paths={},
        )

    monkeypatch.setattr(cli, "run_fast_gate", fake_run_fast_gate)
    code = cli.main(
        ["--root", str(fake_env.root), "fast-gate"], ctx_factory=fake_env.factory
    )
    assert code == 1


def test_ids_with_status_unit(fake_env: SimpleNamespace) -> None:
    ctx = _ctx(fake_env)
    first = _seed(fake_env, 1, {"hyperopt": "passed"})
    second = _seed(fake_env, 2, {"hyperopt": "failed"})
    third = _seed(fake_env, 3, {"hyperopt": "passed"})
    assert cli._ids_with_status(ctx, "hyperopt", "passed") == [first, third]
    assert cli._ids_with_status(ctx, "hyperopt", "failed") == [second]
    assert cli._ids_with_status(ctx, "hyperopt", "nope") == []


def test_validate_derives_ids_from_store(
    fake_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(fake_env)
    assert ctx is not None
    passed_id = _seed(fake_env, 1, {"hyperopt": "passed"})
    _seed(fake_env, 2, {"hyperopt": "failed"})
    captured: dict[str, object] = {}

    def fake_run_validation(ctx: object, ids: Sequence[int]) -> dict[int, str]:
        captured["ids"] = list(ids)
        return {int(i): "passed" for i in ids}

    monkeypatch.setattr(cli, "run_validation", fake_run_validation)
    code = cli.main(
        ["--root", str(fake_env.root), "validate"], ctx_factory=fake_env.factory
    )
    assert code == 0
    assert captured["ids"] == [passed_id]

    def fake_ids_override(ctx: object, ids: Sequence[int]) -> dict[int, str]:
        captured["ids"] = list(ids)
        return {int(i): "passed" for i in ids}

    monkeypatch.setattr(cli, "run_validation", fake_ids_override)
    code = cli.main(
        ["--root", str(fake_env.root), "validate", "--ids", "99"],
        ctx_factory=fake_env.factory,
    )
    assert code == 0
    assert captured["ids"] == [99]


def test_stage_exit_codes_failed_vs_rejected(
    fake_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(fake_env, 1, {"hyperopt": "passed"})

    def fake_failed(ctx: object, ids: Sequence[int]) -> dict[int, str]:
        return {int(i): "failed: boom" for i in ids}

    monkeypatch.setattr(cli, "run_validation", fake_failed)
    assert (
        cli.main(["--root", str(fake_env.root), "validate"],
                 ctx_factory=fake_env.factory)
        == 1
    )

    def fake_rejected(ctx: object, ids: Sequence[int]) -> dict[int, str]:
        return {int(i): "rejected: filters" for i in ids}

    monkeypatch.setattr(cli, "run_validation", fake_rejected)
    assert (
        cli.main(["--root", str(fake_env.root), "validate"],
                 ctx_factory=fake_env.factory)
        == 0
    )


def test_hyperopt_uses_shortlist(
    fake_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    shortlisted = _seed(fake_env, 5, {"fast_gate": "shortlisted"})
    _seed(fake_env, 6, {"fast_gate": "rejected"})
    captured: dict[str, object] = {}

    def fake_run_hyperopt(
        ctx: object, ids: Sequence[int], *, epochs: int | None = None
    ) -> dict[int, str]:
        captured["ids"] = list(ids)
        captured["epochs"] = epochs
        return {int(i): "passed" for i in ids}

    monkeypatch.setattr(cli, "run_hyperopt", fake_run_hyperopt)
    code = cli.main(
        ["--root", str(fake_env.root), "hyperopt", "--epochs", "7"],
        ctx_factory=fake_env.factory,
    )
    assert code == 0
    assert captured["ids"] == [shortlisted]
    assert captured["epochs"] == 7


def test_empty_id_list_skips_stage(
    fake_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    called: list[Sequence[int]] = []

    def fake_run_hyperopt(
        ctx: object, ids: Sequence[int], *, epochs: int | None = None
    ) -> dict[int, str]:
        called.append(list(ids))
        return {}

    monkeypatch.setattr(cli, "run_hyperopt", fake_run_hyperopt)
    code = cli.main(
        ["--root", str(fake_env.root), "hyperopt"], ctx_factory=fake_env.factory
    )
    assert code == 0
    assert called == []
    assert "no candidates" in capsys.readouterr().out


# --------------------------------------------------------------------------- run sequence


def test_run_stops_when_shortlist_empty(
    fake_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    gate_calls: list[object] = []
    hyperopt_calls: list[object] = []

    def fake_gate(ctx: object, *, count: int | None = None) -> FastGateResult:
        gate_calls.append(count)
        return FastGateResult(
            generated=0, backtested=0, failed_runs=0, rejected=0,
            shortlisted=(), scores={}, artifact_paths={},
        )

    def fake_hyperopt(
        ctx: object, ids: Sequence[int], *, epochs: int | None = None
    ) -> dict[int, str]:
        hyperopt_calls.append(list(ids))
        return {}

    monkeypatch.setattr(cli, "run_fast_gate", fake_gate)
    monkeypatch.setattr(cli, "run_hyperopt", fake_hyperopt)
    code = cli.main(["--root", str(fake_env.root), "run"], ctx_factory=fake_env.factory)
    assert code == 0
    assert len(gate_calls) == 1  # audit (ok=False, no data) is warn-only: gate still ran
    assert hyperopt_calls == []  # stopped: empty shortlist
    lines = capsys.readouterr().out.strip().splitlines()
    summary = json.loads(lines[-1])["run_summary"]
    assert "fast-gate" in summary and "data-audit" in summary
    assert summary["data-audit"]["ok"] is False


def test_run_full_chain(
    fake_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    candidate_id = _seed(fake_env, 1)
    seen: dict[str, list[int]] = {}

    def fake_gate(ctx: object, *, count: int | None = None) -> FastGateResult:
        ctx.store.set_stage_status(candidate_id, "fast_gate", "shortlisted")
        return FastGateResult(
            generated=1, backtested=1, failed_runs=0, rejected=0,
            shortlisted=(candidate_id,), scores={candidate_id: 2.0},
            artifact_paths={candidate_id: "z.zip"},
        )

    def _passed(name: str) -> object:
        def fake(ctx: object, ids: Sequence[int], **kwargs: object) -> dict[int, str]:
            seen[name] = list(ids)
            return {int(i): "passed" for i in ids}

        return fake

    monkeypatch.setattr(cli, "run_fast_gate", fake_gate)
    monkeypatch.setattr(cli, "run_hyperopt", _passed("hyperopt"))
    monkeypatch.setattr(cli, "run_validation", _passed("validate"))
    monkeypatch.setattr(cli, "run_bias_checks", _passed("bias"))
    monkeypatch.setattr(cli, "run_walk_forward", _passed("walk-forward"))
    monkeypatch.setattr(cli, "run_robustness", _passed("robust"))
    monkeypatch.setattr(cli, "run_final", _passed("final"))

    code = cli.main(["--root", str(fake_env.root), "run"], ctx_factory=fake_env.factory)
    assert code == 0
    for name in ("hyperopt", "validate", "bias", "walk-forward", "robust", "final"):
        assert seen[name] == [candidate_id], name
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["run_summary"]
    assert set(summary) >= {
        "data-audit", "fast-gate", "hyperopt", "validate",
        "bias", "walk-forward", "robust", "final",
    }


def test_run_reports_failure_exit_code(
    fake_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate_id = _seed(fake_env, 1)

    def fake_gate(ctx: object, *, count: int | None = None) -> FastGateResult:
        ctx.store.set_stage_status(candidate_id, "fast_gate", "shortlisted")
        return FastGateResult(
            generated=1, backtested=1, failed_runs=0, rejected=0,
            shortlisted=(candidate_id,), scores={}, artifact_paths={},
        )

    def fake_hyperopt(
        ctx: object, ids: Sequence[int], *, epochs: int | None = None
    ) -> dict[int, str]:
        return {int(i): "failed: boom" for i in ids}

    monkeypatch.setattr(cli, "run_fast_gate", fake_gate)
    monkeypatch.setattr(cli, "run_hyperopt", fake_hyperopt)
    code = cli.main(["--root", str(fake_env.root), "run"], ctx_factory=fake_env.factory)
    assert code == 1


# --------------------------------------------------------------------------- data/report/status


def test_data_audit_exit_codes(fake_env: SimpleNamespace) -> None:
    # No feather files in the tmp tree -> audit not ok -> exit 1.
    code = cli.main(
        ["--root", str(fake_env.root), "data-audit"], ctx_factory=fake_env.factory
    )
    assert code == 1


def test_data_download_derives_timerange(
    fake_env: SimpleNamespace, capsys: pytest.CaptureFixture
) -> None:
    code = cli.main(
        ["--root", str(fake_env.root), "data-download"], ctx_factory=fake_env.factory
    )
    assert code == 0
    assert len(fake_env.runner.calls) == 1
    argv = list(fake_env.runner.calls[0][0])
    assert argv[:2] == ["download-data", "--config"]
    assert argv[argv.index("--timerange") + 1] == "20220101-20230630"
    assert "BTC/USDT:USDT" in argv
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 0


def test_generate_writes_manifest(
    fake_env: SimpleNamespace, capsys: pytest.CaptureFixture
) -> None:
    code = cli.main(
        ["--root", str(fake_env.root), "generate", "--count", "2"],
        ctx_factory=fake_env.factory,
    )
    assert code == 0
    manifest = fake_env.root / "output" / "manifests" / "latest_generation.json"
    assert manifest.is_file()
    assert json.loads(capsys.readouterr().out)["manifest"] == str(manifest)


def test_report_writes_markdown(
    fake_env: SimpleNamespace, capsys: pytest.CaptureFixture
) -> None:
    _seed(
        fake_env, 1,
        {"fast_gate": "shortlisted", "hyperopt": "passed"},
        metrics=dict(TRAIN_METRICS),
    )
    code = cli.main(
        ["--root", str(fake_env.root), "report"], ctx_factory=fake_env.factory
    )
    assert code == 0
    report = fake_env.root / "output" / "report.md"
    assert report.is_file()
    text = report.read_text(encoding="utf-8")
    assert "Miner_000001" in text
    assert "shortlisted" in text and "passed" in text
    assert "trades=10" in text
    assert json.loads(capsys.readouterr().out)["report"] == str(report)


def test_status_counts(fake_env: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    _seed(fake_env, 1, {"fast_gate": "shortlisted"})
    _seed(fake_env, 2, {"fast_gate": "rejected"})
    code = cli.main(
        ["--root", str(fake_env.root), "status"], ctx_factory=fake_env.factory
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidates"] == 2
    assert payload["counts"]["fast_gate"] == {"shortlisted": 1, "rejected": 1}


def test_status_bare_root_without_crash(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    code = cli.main(["--root", str(bare), "status"])
    assert code == 0
