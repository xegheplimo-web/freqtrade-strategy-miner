"""Robustness — pipeline stage 8 (WORKFLOW.md).

Stress each candidate on the VALIDATION timerange with three checks:

- (A) fee stress: backtest with ``--fee <base_fee * fee_multiplier>`` (freqtrade
  applies the fee twice, at entry and exit, so raising it approximates
  slippage — freqtrade exposes no slippage flag).
- (B) parameter perturbation: ``perturb_runs`` seeded neighbours (±``perturb_pct``)
  backtested from ``<user_data>/strategies/robustness/``; passes when at least
  two thirds of the neighbours stay profitable. This searches for a stable
  plateau instead of a single sharp parameter peak.
- (C) drop-top-trades: drop the ``drop_top_frac`` most profitable trades of the
  latest validation artifact; passes when the remainder stays profitable with
  no severe profit-factor collapse (fragility check).

Persistence choice: the full per-candidate detail is written as JSON to
``output/robustness/<Class>.json`` (created on demand). Run rows keep only the
short ``check=...`` notes.

OUT OF SCOPE for v1 (future work): multi-regime splits, multi-pair expansion,
entry-delay simulation and explicit slippage modelling beyond fee stress.
"""

from __future__ import annotations

import json
import math
import random
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from strategy_miner import params as params_mod
from strategy_miner import results as results_mod
from strategy_miner.results import StrategyStats
from strategy_miner.runner import CONTAINER_USERDATA

from . import PipelineContext, backtest_args, find_latest_artifact, record_stage_run

STAGE = "robustness"

ROBUSTNESS_DIR_NAME = "robustness"
ROBUSTNESS_CONTAINER = f"{CONTAINER_USERDATA}/strategies/{ROBUSTNESS_DIR_NAME}"

DEFAULTS = {
    "base_fee": 0.0005,
    "fee_multiplier": 1.5,
    "perturb_pct": 0.07,
    "perturb_runs": 3,
    "drop_top_frac": 0.05,
}


@dataclass
class RobustnessResult:
    """Per-candidate outcome of the three robustness checks.

    ``fee``/``perturb``/``drop_top`` are plain-JSON metric subsets; ``status``
    is ``"passed"`` when all three checks pass, else
    ``"rejected: <failed check names>"``.
    """

    candidate_id: int
    class_name: str
    fee: dict[str, Any] = field(default_factory=dict)
    perturb: dict[str, Any] = field(default_factory=dict)
    drop_top: dict[str, Any] = field(default_factory=dict)
    status: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of this result."""
        return asdict(self)


def _robustness_cfg(ctx: PipelineContext) -> dict[str, Any]:
    """Merge ``miner_cfg["robustness"]`` over the frozen defaults."""
    cfg = dict(DEFAULTS)
    raw = ctx.miner_cfg.get("robustness", {})
    if isinstance(raw, dict):
        cfg.update(raw)
    return cfg


def _robustness_backtest_args(
    ctx: PipelineContext, class_name: str, timerange: str, py_dir: str | Path
) -> list[str]:
    """Backtest argv shaped like :func:`backtest_args` with an overridden strategy-path.

    ``py_dir`` is the container strategy-path (e.g. ``ROBUSTNESS_CONTAINER``);
    a host :class:`Path` under ``user_data`` is mapped via ``runner.map_path``.
    """
    if isinstance(py_dir, Path):
        strategy_path = ctx.runner.map_path(py_dir)
    else:
        strategy_path = str(py_dir)
    return [
        "backtesting",
        "--config", ctx.ft_config_container,
        "--strategy", class_name,
        "--strategy-path", strategy_path,
        "--timerange", timerange,
        "--export", "trades",
    ]


def _select_stats(parsed: dict[str, StrategyStats], class_name: str) -> StrategyStats:
    """Pick the StrategyStats for ``class_name`` (fallback: the only/first strategy)."""
    if class_name in parsed:
        return parsed[class_name]
    if len(parsed) == 1:
        return next(iter(parsed.values()))
    return parsed[sorted(parsed)[0]]


def _summary_numbers(summary: dict[str, Any]) -> tuple[float, Any]:
    """Return ``(profit_total, profit_factor)`` with ``0.0`` fallbacks for Nones."""
    profit_total = summary.get("profit_total")
    profit_factor = summary.get("profit_factor")
    if profit_total is None:
        profit_total = 0.0
    return float(profit_total), profit_factor


def _run_and_parse(
    ctx: PipelineContext,
    candidate_id: int,
    args: Sequence[str],
    log_name: str,
    class_name: str,
    notes: str,
) -> tuple[Any, StrategyStats | None, str | None]:
    """Run backtest ``args``, record the run, and parse the produced artifact.

    Returns ``(result, stats_or_None, artifact_str_or_None)``. The run is always
    recorded (stage ``robustness``) so failures stay visible in the store.
    """
    since_ts = time.time()
    log_dir = ctx.root / "agent_logs"
    result = ctx.runner.run(list(args), log_dir=log_dir, log_name=log_name)
    if result.exit_code != 0:
        record_stage_run(ctx, candidate_id, STAGE, args, result, notes=notes)
        return result, None, None
    artifact = find_latest_artifact(ctx, since_ts)
    if artifact is None:
        record_stage_run(ctx, candidate_id, STAGE, args, result, notes=notes)
        return result, None, None
    try:
        stats = _select_stats(results_mod.parse_backtest_result(artifact), class_name)
    except (OSError, ValueError, KeyError):
        record_stage_run(
            ctx, candidate_id, STAGE, args, result,
            artifact_path=str(artifact), notes=notes,
        )
        return result, None, None
    profit_total, profit_factor = _summary_numbers(stats.summary)
    record_stage_run(
        ctx, candidate_id, STAGE, args, result,
        artifact_path=str(artifact),
        metrics={"profit_total": profit_total, "profit_factor": profit_factor},
        notes=notes,
    )
    return result, stats, str(artifact)


def _fee_check(
    ctx: PipelineContext,
    candidate_id: int,
    class_name: str,
    timerange: str,
    base_fee: float,
    fee_multiplier: float,
) -> dict[str, Any]:
    """Check A: backtest with stressed fee; pass iff profit>0 and pf>=1.0."""
    fee = base_fee * fee_multiplier
    args = backtest_args(ctx, class_name, timerange) + ["--fee", str(fee)]
    _result, stats, artifact = _run_and_parse(
        ctx, candidate_id, args, f"robustness_fee_{class_name}.log",
        class_name, "check=fee",
    )
    if stats is None:
        return {
            "fee": fee, "profit_total": 0.0, "profit_factor": 0.0,
            "passed": False, "artifact": artifact,
        }
    profit_total, profit_factor = _summary_numbers(stats.summary)
    pf = 0.0 if profit_factor is None else float(profit_factor)
    passed = bool(profit_total > 0 and pf >= 1.0)
    return {
        "fee": fee, "profit_total": profit_total, "profit_factor": profit_factor,
        "passed": passed, "artifact": artifact,
    }


def _variant_source(source: str, class_name: str, variant: str) -> str:
    """Copy strategy ``source`` while renaming the class to ``variant``.

    Falls back to the verbatim source when the class declaration is not found
    (keeps the helper total even for hand-written fixtures).
    """
    pattern = re.compile(r"(class\s+)" + re.escape(class_name) + r"(\s*[\(:])")
    renamed, count = pattern.subn(r"\1" + variant + r"\2", source, count=1)
    return renamed if count else source


def _perturb_check(
    ctx: PipelineContext,
    candidate_id: int,
    class_name: str,
    timerange: str,
    perturb_pct: float,
    perturb_runs: int,
) -> dict[str, Any]:
    """Check B: seeded param neighbours; pass iff >= ceil(0.66*runs) stay robust."""
    host_dir = ctx.user_data / "strategies" / ROBUSTNESS_DIR_NAME
    host_dir.mkdir(parents=True, exist_ok=True)
    try:
        original = params_mod.read_params_file(
            ctx.generated_dir / f"{class_name}.json"
        )
    except (OSError, ValueError, KeyError):
        return {
            "passed_runs": 0, "runs": int(perturb_runs), "passed": False,
            "note": "no params file", "details": [],
        }
    try:
        source_path = ctx.generated_dir / f"{class_name}.py"
        source = source_path.read_text(encoding="utf-8")
    except OSError:
        return {
            "passed_runs": 0, "runs": int(perturb_runs), "passed": False,
            "note": "no strategy file", "details": [],
        }
    passed_runs = 0
    details: list[dict[str, Any]] = []
    for k in range(int(perturb_runs)):
        seed = candidate_id * 1000 + k
        variant = f"{class_name}_p{k}"
        perturbed = params_mod.perturb_params(
            original, float(perturb_pct), rng=random.Random(seed)
        )
        dest_py = host_dir / f"{variant}.py"
        dest_py.write_text(
            _variant_source(source, class_name, variant), encoding="utf-8"
        )
        params_mod.write_params_file(dest_py, perturbed, strategy_name=variant)
        args = _robustness_backtest_args(ctx, variant, timerange, ROBUSTNESS_CONTAINER)
        _result, stats, artifact = _run_and_parse(
            ctx, candidate_id, args, f"robustness_perturb_{variant}.log",
            variant, f"check=perturb k={k} seed={seed}",
        )
        if stats is not None:
            profit_total, profit_factor = _summary_numbers(stats.summary)
            pf = 0.0 if profit_factor is None else float(profit_factor)
            ok = bool(profit_total > 0 and pf >= 0.8)
        else:
            profit_total, profit_factor, ok = 0.0, 0.0, False
        if ok:
            passed_runs += 1
        details.append({
            "k": k, "seed": seed, "variant": variant,
            "profit_total": profit_total, "profit_factor": profit_factor,
            "passed": ok, "artifact": artifact,
        })
    required = math.ceil(0.66 * int(perturb_runs)) if int(perturb_runs) > 0 else 0
    return {
        "passed_runs": passed_runs, "runs": int(perturb_runs),
        "required": required, "passed": passed_runs >= required and int(perturb_runs) > 0,
        "details": details,
    }


def _drop_top_check(
    ctx: PipelineContext,
    candidate_id: int,
    class_name: str,
    drop_top_frac: float,
) -> dict[str, Any]:
    """Check C: drop the top ``n`` trades of the latest validation artifact."""
    validation_runs = [
        r for r in ctx.store.runs_for(candidate_id, "validation")
        if r.get("artifact_path")
    ]
    if not validation_runs:
        return {"passed": False, "note": "no validation artifact"}
    artifact_path = Path(validation_runs[-1]["artifact_path"])
    if not artifact_path.is_absolute():
        artifact_path = ctx.root / artifact_path
    try:
        data = results_mod.load_backtest_json(artifact_path)
        parsed = results_mod.parse_backtest_result(artifact_path)
        _ = data  # parsed from the same artifact; data kept for clarity
        stats = _select_stats(parsed, class_name)
    except (OSError, ValueError, KeyError) as exc:
        return {"passed": False, "note": f"unreadable validation artifact: {exc}"}
    trades = list(stats.trades)
    n = max(5, math.ceil(float(drop_top_frac) * len(trades)))
    outcome = results_mod.drop_top_n_trades(trades, n)
    profit_before = stats.summary.get("profit_total_abs")
    if profit_before is None:
        profit_before = float(sum(t.get("profit_abs", 0.0) for t in trades))
    pf_before = stats.summary.get("profit_factor")
    pf_after = outcome["profit_factor_after"]
    if pf_before is None:
        pf_ok = True
    elif pf_after is None:
        pf_ok = True
    else:
        pf_ok = bool(float(pf_after) >= 0.8 * float(pf_before))
    passed = bool(outcome["profit_abs_after"] > 0 and pf_ok)
    return {
        "n": n, "trades": len(trades),
        "profit_abs_after": outcome["profit_abs_after"],
        "profit_factor_after": outcome["profit_factor_after"],
        "profit_factor_before": pf_before,
        "profit_abs_before": float(profit_before),
        "passed": passed,
    }


def _persist_result(ctx: PipelineContext, result: RobustnessResult) -> Path:
    """Write the full detail JSON to ``output/robustness/<Class>.json``."""
    out_dir = ctx.root / "output" / STAGE
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result.class_name}.json"
    path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return path


def run_robustness(
    ctx: PipelineContext, candidate_ids: Sequence[int]
) -> dict[int, str]:
    """Run stage-8 robustness for each candidate; return ``{candidate_id: status}``.

    ``status`` is ``"passed"`` when fee, perturb and drop-top checks all pass,
    else ``"rejected: <failed check names>"``. Exactly one
    ``set_stage_status("robustness", ...)`` call happens per candidate.
    """
    cfg = _robustness_cfg(ctx)
    timerange = str(ctx.miner_cfg["validation_timerange"])
    statuses: dict[int, str] = {}
    for candidate_id in candidate_ids:
        row = ctx.store.get_candidate(candidate_id)
        class_name = str(row["class_name"])
        fee = _fee_check(
            ctx, candidate_id, class_name, timerange,
            float(cfg["base_fee"]), float(cfg["fee_multiplier"]),
        )
        perturb = _perturb_check(
            ctx, candidate_id, class_name, timerange,
            float(cfg["perturb_pct"]), int(cfg["perturb_runs"]),
        )
        drop_top = _drop_top_check(
            ctx, candidate_id, class_name, float(cfg["drop_top_frac"])
        )
        failed = [
            name for name, check in
            (("fee", fee), ("perturb", perturb), ("drop_top", drop_top))
            if not check.get("passed", False)
        ]
        status = "passed" if not failed else f"rejected: {', '.join(failed)}"
        result = RobustnessResult(
            candidate_id=candidate_id, class_name=class_name,
            fee=fee, perturb=perturb, drop_top=drop_top, status=status,
        )
        _persist_result(ctx, result)
        ctx.store.set_stage_status(candidate_id, STAGE, status)
        statuses[candidate_id] = status
    return statuses
