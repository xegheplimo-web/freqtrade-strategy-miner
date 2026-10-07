"""Unit tests for the pipeline foundation (no docker, no network)."""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path

from strategy_miner.generator import GenomeGenerator
from strategy_miner.pipeline import (
    backtest_args,
    find_latest_artifact,
    make_context,
    record_stage_run,
    sync_ft_config,
)
from strategy_miner.runner import CONTAINER_USERDATA, DockerRunner, RunResult
from strategy_miner.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_CONFIG = json.loads((REPO_ROOT / "config" / "miner.json").read_text(encoding="utf-8"))


def _make_root(tmp_path: Path, *, config: dict | None = None) -> Path:
    """Create a minimal worktree: config/miner.json + freqtrade/config.example.json."""
    (tmp_path / "config").mkdir()
    cfg = SAMPLE_CONFIG if config is None else config
    (tmp_path / "config" / "miner.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    (tmp_path / "freqtrade").mkdir()
    shutil.copyfile(
        REPO_ROOT / "freqtrade" / "config.example.json",
        tmp_path / "freqtrade" / "config.example.json",
    )
    return tmp_path


def _make_zip(results_dir: Path, name: str, *, mtime: float) -> Path:
    """Create a dummy backtest zip with a controlled mtime."""
    path = results_dir / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("backtest-result-dummy.json", "{}")
    os.utime(path, (mtime, mtime))
    return path


class TestMakeContext:
    def test_defaults(self, tmp_path: Path) -> None:
        root = _make_root(tmp_path)
        ctx = make_context(root)
        user_data = root / "freqtrade" / "user_data"
        assert ctx.root == root
        assert ctx.miner_cfg == SAMPLE_CONFIG
        assert ctx.user_data == user_data
        assert ctx.generated_dir == user_data / "strategies" / "generated"
        assert ctx.results_dir == user_data / "backtest_results"
        assert ctx.ft_config_container == f"{CONTAINER_USERDATA}/config.miner.json"
        assert ctx.generated_dir_container == f"{CONTAINER_USERDATA}/strategies/generated"
        assert ctx.results_dir_container == f"{CONTAINER_USERDATA}/backtest_results"
        assert isinstance(ctx.runner, DockerRunner)
        assert ctx.runner.user_data_dir == user_data
        assert isinstance(ctx.store, Store)
        assert ctx.store.db_path == root / "output" / "miner.db"
        assert ctx.generated_dir.is_dir()
        assert ctx.results_dir.is_dir()

    def test_custom_runner_and_db(self, tmp_path: Path) -> None:
        root = _make_root(tmp_path)
        runner = object()
        db_path = tmp_path / "elsewhere" / "custom.db"
        ctx = make_context(root, runner=runner, db_path=db_path)
        assert ctx.runner is runner
        assert ctx.store.db_path == db_path

    def test_config_path_override(self, tmp_path: Path) -> None:
        root = _make_root(tmp_path)
        custom = dict(SAMPLE_CONFIG, seed=42, candidate_count=7)
        custom_path = tmp_path / "custom.json"
        custom_path.write_text(json.dumps(custom), encoding="utf-8")
        ctx = make_context(root, config_path=custom_path)
        assert ctx.miner_cfg == custom


class TestBacktestArgs:
    def test_exact(self, tmp_path: Path) -> None:
        ctx = make_context(_make_root(tmp_path))
        args = backtest_args(ctx, "Miner_000001", "20220101-20241231")
        assert args == [
            "backtesting",
            "--config", f"{CONTAINER_USERDATA}/config.miner.json",
            "--strategy", "Miner_000001",
            "--strategy-path", f"{CONTAINER_USERDATA}/strategies/generated",
            "--timerange", "20220101-20241231",
            "--export", "trades",
        ]

    def test_export_override(self, tmp_path: Path) -> None:
        ctx = make_context(_make_root(tmp_path))
        args = backtest_args(ctx, "Miner_000001", "20220101-20241231", export="none")
        assert args[-2:] == ["--export", "none"]


class TestSyncFtConfig:
    def test_valid_json_and_expected_keys(self, tmp_path: Path) -> None:
        ctx = make_context(_make_root(tmp_path))
        path = sync_ft_config(ctx)
        assert path == ctx.user_data / "config.miner.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["exchange"]["name"] == SAMPLE_CONFIG["exchange"]
        assert data["exchange"]["pair_whitelist"] == SAMPLE_CONFIG["pairs"]
        assert data["trading_mode"] == "futures"
        assert data["margin_mode"] == "isolated"
        assert data["stake_currency"] == "USDT"
        assert data["timeframe"] == SAMPLE_CONFIG["timeframe"]

    def test_spot_mode_drops_margin_mode(self, tmp_path: Path) -> None:
        cfg = dict(SAMPLE_CONFIG, trading_mode="spot")
        ctx = make_context(_make_root(tmp_path, config=cfg))
        data = json.loads(sync_ft_config(ctx).read_text(encoding="utf-8"))
        assert data["trading_mode"] == "spot"
        assert "margin_mode" not in data

    def test_reflects_custom_config(self, tmp_path: Path) -> None:
        cfg = dict(
            SAMPLE_CONFIG,
            exchange="kucoin",
            pairs=["BTC/USDT:USDT"],
            timeframe="1h",
        )
        ctx = make_context(_make_root(tmp_path, config=cfg))
        data = json.loads(sync_ft_config(ctx).read_text(encoding="utf-8"))
        assert data["exchange"]["name"] == "kucoin"
        assert data["exchange"]["pair_whitelist"] == ["BTC/USDT:USDT"]
        assert data["timeframe"] == "1h"


class TestFindLatestArtifact:
    T0 = 1_000_000_000.0
    T1 = 1_000_000_100.0
    T2 = 1_000_000_200.0
    SINCE = 1_000_000_050.0

    def test_last_result_preferred(self, tmp_path: Path) -> None:
        ctx = make_context(_make_root(tmp_path))
        older = _make_zip(ctx.results_dir, "backtest-result-older.zip", mtime=self.T1)
        _make_zip(ctx.results_dir, "backtest-result-newer.zip", mtime=self.T2)
        (ctx.results_dir / ".last_result.json").write_text(
            json.dumps({"latest_backtest": older.name}), encoding="utf-8"
        )
        # .last_result.json wins even though a newer zip exists.
        assert find_latest_artifact(ctx, self.SINCE) == older

    def test_last_result_stale_falls_back_to_newest_zip(self, tmp_path: Path) -> None:
        ctx = make_context(_make_root(tmp_path))
        stale = _make_zip(ctx.results_dir, "backtest-result-stale.zip", mtime=self.T0)
        newer = _make_zip(ctx.results_dir, "backtest-result-newer.zip", mtime=self.T2)
        (ctx.results_dir / ".last_result.json").write_text(
            json.dumps({"latest_backtest": stale.name}), encoding="utf-8"
        )
        # stale zip mtime < since_ts -> newest zip with mtime >= since_ts.
        assert find_latest_artifact(ctx, self.SINCE) == newer

    def test_last_result_pointing_at_missing_zip_falls_back(self, tmp_path: Path) -> None:
        ctx = make_context(_make_root(tmp_path))
        newer = _make_zip(ctx.results_dir, "backtest-result-newer.zip", mtime=self.T2)
        (ctx.results_dir / ".last_result.json").write_text(
            json.dumps({"latest_backtest": "backtest-result-gone.zip"}), encoding="utf-8"
        )
        assert find_latest_artifact(ctx, self.SINCE) == newer

    def test_fallback_newest_zip_within_since(self, tmp_path: Path) -> None:
        ctx = make_context(_make_root(tmp_path))
        _make_zip(ctx.results_dir, "backtest-result-older.zip", mtime=self.T0)
        newer = _make_zip(ctx.results_dir, "backtest-result-newer.zip", mtime=self.T2)
        assert find_latest_artifact(ctx, self.SINCE) == newer

    def test_best_effort_newest_overall(self, tmp_path: Path) -> None:
        ctx = make_context(_make_root(tmp_path))
        _make_zip(ctx.results_dir, "backtest-result-older.zip", mtime=self.T0)
        newer = _make_zip(ctx.results_dir, "backtest-result-newer.zip", mtime=self.T2)
        # since_ts in the future: no zip qualifies -> newest overall.
        assert find_latest_artifact(ctx, self.T2 + 1000.0) == newer

    def test_none_when_no_zips(self, tmp_path: Path) -> None:
        ctx = make_context(_make_root(tmp_path))
        assert find_latest_artifact(ctx, self.SINCE) is None

    def test_none_when_only_config_json(self, tmp_path: Path) -> None:
        ctx = make_context(_make_root(tmp_path))
        (ctx.results_dir / "backtest-result-x_config.json").write_text("{}", encoding="utf-8")
        assert find_latest_artifact(ctx, self.SINCE) is None


class TestRecordStageRun:
    def test_writes_run_to_store(self, tmp_path: Path) -> None:
        ctx = make_context(_make_root(tmp_path))
        genome = GenomeGenerator(seed=1).generate_one(1)
        candidate_id = ctx.store.upsert_candidate(
            genome, "freqtrade/user_data/strategies/generated/Miner_000001.py"
        )
        result = RunResult(
            argv=("backtesting", "--config", "/freqtrade/user_data/config.miner.json"),
            exit_code=0,
            stdout="",
            duration_s=1.5,
        )
        run_id = record_stage_run(
            ctx,
            candidate_id,
            "fast_gate",
            ["backtesting", "--config", "/freqtrade/user_data/config.miner.json"],
            result,
            artifact_path="/tmp/backtest-result-x.zip",
            metrics={"trades": 4705},
            notes="ok",
        )
        assert isinstance(run_id, int)
        runs = ctx.store.runs_for(candidate_id, "fast_gate")
        assert len(runs) == 1
        assert runs[0]["id"] == run_id
        assert runs[0]["argv"] == list(result.argv)
        assert runs[0]["exit_code"] == 0
        assert runs[0]["duration_s"] == 1.5
        assert runs[0]["artifact_path"] == "/tmp/backtest-result-x.zip"
        assert runs[0]["metrics"] == {"trades": 4705}
        assert runs[0]["notes"] == "ok"
