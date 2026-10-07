"""Pipeline package foundation: PipelineContext and shared stage helpers.

FROZEN contract: .orchestrator/INTERFACES.md section 7 (2026-10-08). The pipeline
package layers stages (fast gate, hyperopt, validation, ...) on top of the frozen
foundation modules (runner/results/store/params/data) without re-implementing them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from strategy_miner.orchestrator import load_config
from strategy_miner.runner import CONTAINER_USERDATA, DockerRunner, Runner, RunResult
from strategy_miner.store import Store

# Name of the freqtrade runtime config inside the container user data.
FT_CONFIG_NAME = "config.miner.json"


@dataclass(frozen=True)
class PipelineContext:
    """Everything a pipeline stage needs (FROZEN shape, INTERFACES.md §7)."""

    root: Path                    # repo root
    miner_cfg: dict               # parsed config/miner.json
    runner: Runner
    store: Store
    user_data: Path               # <root>/freqtrade/user_data
    generated_dir: Path           # user_data/strategies/generated
    results_dir: Path             # user_data/backtest_results
    ft_config_container: str      # /freqtrade/user_data/config.miner.json
    generated_dir_container: str  # /freqtrade/user_data/strategies/generated
    results_dir_container: str    # /freqtrade/user_data/backtest_results


def make_context(
    root: Path | str,
    *,
    config_path: Path | str | None = None,
    runner: Runner | None = None,
    db_path: Path | str | None = None,
) -> PipelineContext:
    """Build a PipelineContext for ``root``.

    Defaults: config at ``<root>/config/miner.json``, runner=DockerRunner(user_data),
    store at ``<root>/output/miner.db``. The generated/results directories are
    created if missing and the three container-path strings are set.
    """
    root = Path(root)
    cfg_path = Path(config_path) if config_path is not None else root / "config" / "miner.json"
    cfg = load_config(cfg_path)

    user_data = root / "freqtrade" / "user_data"
    generated_dir = user_data / "strategies" / "generated"
    results_dir = user_data / "backtest_results"
    generated_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    if runner is None:
        runner = DockerRunner(user_data)
    store = Store(Path(db_path) if db_path is not None else root / "output" / "miner.db")

    return PipelineContext(
        root=root,
        miner_cfg=cfg,
        runner=runner,
        store=store,
        user_data=user_data,
        generated_dir=generated_dir,
        results_dir=results_dir,
        ft_config_container=f"{CONTAINER_USERDATA}/{FT_CONFIG_NAME}",
        generated_dir_container=f"{CONTAINER_USERDATA}/strategies/generated",
        results_dir_container=f"{CONTAINER_USERDATA}/backtest_results",
    )


def sync_ft_config(ctx: PipelineContext) -> Path:
    """Write the freqtrade runtime config and return its host path.

    Merges ``freqtrade/config.example.json`` with the miner config values:
    pairs -> ``exchange.pair_whitelist``, ``exchange.name``, ``trading_mode``,
    ``margin_mode`` ("isolated" for futures, removed for spot), ``stake_currency``
    ("USDT") and ``timeframe``. The file lives at
    ``<root>/freqtrade/user_data/config.miner.json`` (gitignored).
    """
    example_path = ctx.root / "freqtrade" / "config.example.json"
    base = json.loads(example_path.read_text(encoding="utf-8"))
    cfg = ctx.miner_cfg

    exchange = base.get("exchange")
    if not isinstance(exchange, dict):
        exchange = {}
        base["exchange"] = exchange
    exchange["name"] = cfg["exchange"]
    exchange["pair_whitelist"] = list(cfg["pairs"])

    base["trading_mode"] = cfg["trading_mode"]
    if cfg["trading_mode"] == "futures":
        base["margin_mode"] = "isolated"
    else:
        base.pop("margin_mode", None)
    base["stake_currency"] = "USDT"
    base["timeframe"] = cfg["timeframe"]

    path = ctx.user_data / FT_CONFIG_NAME
    path.write_text(json.dumps(base, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def backtest_args(
    ctx: PipelineContext, class_name: str, timerange: str, *, export: str = "trades"
) -> list[str]:
    """Exact freqtrade backtesting argv (all container paths)."""
    return [
        "backtesting",
        "--config", ctx.ft_config_container,
        "--strategy", class_name,
        "--strategy-path", ctx.generated_dir_container,
        "--timerange", timerange,
        "--export", export,
    ]


def find_latest_artifact(ctx: PipelineContext, since_ts: float) -> Path | None:
    """Locate the backtest artifact produced after ``since_ts`` (epoch seconds).

    Preference order: the zip named by ``.last_result.json`` when its mtime is
    >= ``since_ts``; else the newest ``backtest-result-*.zip`` with mtime
    >= ``since_ts``; else the newest zip overall (best effort). Returns None when
    no zip exists at all. ``*_config.json`` files are never returned.
    """
    results_dir = ctx.results_dir

    last_result = results_dir / ".last_result.json"
    if last_result.is_file():
        try:
            payload = json.loads(last_result.read_text(encoding="utf-8"))
            zip_name = payload["latest_backtest"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            zip_name = None
        if isinstance(zip_name, str) and zip_name:
            candidate = results_dir / zip_name
            if (
                candidate.is_file()
                and candidate.suffix == ".zip"
                and not candidate.name.endswith("_config.json")
                and candidate.stat().st_mtime >= since_ts
            ):
                return candidate

    zips = sorted(
        (p for p in results_dir.glob("backtest-result-*.zip") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in zips:
        if path.stat().st_mtime >= since_ts:
            return path
    return zips[0] if zips else None


def record_stage_run(
    ctx: PipelineContext,
    candidate_id: int,
    stage: str,
    args: Sequence[str],
    result: RunResult,
    *,
    artifact_path: str | None = None,
    metrics: dict | None = None,
    notes: str | None = None,
) -> int:
    """Record a stage run via the Store (thin wrapper; stages never hand-roll store calls)."""
    return ctx.store.record_run(
        candidate_id,
        stage,
        argv=result.argv,
        exit_code=result.exit_code,
        duration_s=result.duration_s,
        artifact_path=artifact_path,
        metrics=metrics,
        notes=notes,
    )

