"""Refine stages — hyperopt (stage 4) and validation (stage 5) (WORKFLOW.md).

Stage 4 runs freqtrade hyperopt over the TRAIN timerange for each shortlisted
candidate. Freqtrade auto-exports the best params to a ``<Class>.json`` sidecar
next to the strategy file (INTERFACES.md header note); the sidecar is validated
via ``params.read_params_file`` and recorded as the candidate's ``params_path``.

Stage 5 backtests each hyperopted candidate on the unseen VALIDATION timerange
(the params sidecar is auto-applied by freqtrade — no extra argv flags) and
rejects candidates that fail the hard filters again or degrade sharply versus
their TRAIN (fast-gate) metrics.

Runs without docker in tests via any object implementing the ``Runner``
protocol (see tests/test_pipeline_refine.py::FakeRunner).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import Mapping, Sequence

from strategy_miner import fitness, params
from strategy_miner.genome import StrategyGenome
from strategy_miner.results import StrategyStats, extract_metrics, parse_backtest_result

from . import PipelineContext, backtest_args, find_latest_artifact, record_stage_run

STAGE_HYPEROPT = "hyperopt"
STAGE_VALIDATION = "validation"

DEFAULT_EPOCHS = 300


def hyperopt_args(
    ctx: PipelineContext,
    class_name: str,
    timerange: str,
    *,
    epochs: int,
    min_trades: int | None = None,
    random_state: int | None = None,
) -> list[str]:
    """Exact freqtrade hyperopt argv (all container paths).

    ``min_trades``/``random_state`` are appended only when configured
    (``miner_cfg["hyperopt"]``); unset keeps freqtrade defaults and the
    historical byte-identical argv.
    """
    argv = [
        "hyperopt",
        "--config", ctx.ft_config_container,
        "--strategy", class_name,
        "--strategy-path", ctx.generated_dir_container,
        "--timerange", timerange,
        # No "sell": compiled strategies have no sell params; freqtrade rejects empty spaces
        "--spaces", "buy", "roi", "stoploss",
        "--hyperopt-loss", "MultiMetricHyperOptLoss",
        "-e", str(epochs),
    ]
    if min_trades is not None:
        argv += ["--min-trades", str(int(min_trades))]
    if random_state is not None:
        argv += ["--random-state", str(int(random_state))]
    return argv


def run_hyperopt(
    ctx: PipelineContext, candidate_ids: Sequence[int], *, epochs: int | None = None
) -> dict[int, str]:
    """Run hyperopt on the TRAIN timerange for each candidate (WORKFLOW.md stage 4).

    Returns ``{candidate_id: status}`` where status is ``"passed"`` or
    ``"failed: <reason>"``. On success the exported params sidecar
    (``<generated_dir>/<Class>.json``) is stored as the candidate's
    ``params_path``; the candidate keeps its genome and ``py_path``.
    """
    hyperopt_cfg = ctx.miner_cfg.get("hyperopt", {}) or {}
    if epochs is None:
        epochs = int(hyperopt_cfg.get("epochs", DEFAULT_EPOCHS))
    min_trades = hyperopt_cfg.get("min_trades")
    random_state = hyperopt_cfg.get("random_state")
    timerange = str(ctx.miner_cfg["train_timerange"])
    log_dir = ctx.root / "agent_logs"
    statuses: dict[int, str] = {}

    for candidate_id in candidate_ids:
        row = _candidate_row(ctx, candidate_id)
        if row is None:
            statuses[candidate_id] = "failed: unknown candidate_id"
            continue
        class_name = row["class_name"]
        args = hyperopt_args(
            ctx,
            class_name,
            timerange,
            epochs=epochs,
            min_trades=min_trades,
            random_state=random_state,
        )
        result = ctx.runner.run(
            args, log_dir=log_dir, log_name=f"hyperopt_{class_name}.log"
        )

        if result.exit_code != 0:
            note = f"runner exit_code={result.exit_code}"
            record_stage_run(ctx, candidate_id, STAGE_HYPEROPT, args, result, notes=note)
            ctx.store.set_stage_status(candidate_id, STAGE_HYPEROPT, "failed", note=note)
            statuses[candidate_id] = f"failed: {note}"
            continue

        sidecar = ctx.generated_dir / f"{class_name}.json"
        if not sidecar.is_file():
            note = "params sidecar not found after hyperopt run"
            record_stage_run(ctx, candidate_id, STAGE_HYPEROPT, args, result, notes=note)
            ctx.store.set_stage_status(candidate_id, STAGE_HYPEROPT, "failed", note=note)
            statuses[candidate_id] = f"failed: {note}"
            continue

        try:
            params.read_params_file(sidecar)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            note = f"invalid params sidecar: {exc}"
            record_stage_run(
                ctx, candidate_id, STAGE_HYPEROPT, args, result,
                artifact_path=str(sidecar), notes=note,
            )
            ctx.store.set_stage_status(candidate_id, STAGE_HYPEROPT, "failed", note=note)
            statuses[candidate_id] = f"failed: {note}"
            continue

        genome = _genome_from_row(row)
        ctx.store.upsert_candidate(genome, row["py_path"], _store_path(ctx, sidecar))
        record_stage_run(
            ctx, candidate_id, STAGE_HYPEROPT, args, result,
            artifact_path=str(sidecar), notes=f"epochs={epochs}",
        )
        ctx.store.set_stage_status(candidate_id, STAGE_HYPEROPT, "passed")
        statuses[candidate_id] = "passed"

    return statuses


def run_validation(
    ctx: PipelineContext,
    candidate_ids: Sequence[int],
    *,
    train_metrics: Mapping[int, dict] | None = None,
) -> dict[int, str]:
    """Backtest each candidate on the VALIDATION timerange (WORKFLOW.md stage 5).

    Returns ``{candidate_id: status}`` where status is ``"passed"``,
    ``"rejected: <reason>"`` (a gate did not hold) or ``"failed: <reason>"``
    (runner/artifact problem — same convention as the fast-gate stage).

    Gates (ALL must hold):
      1. ``fitness.passes_hard_filters`` with ``miner_cfg["hard_filters"]`` —
         the same bar as TRAIN;
      2. ``max_drawdown_pct <= 1.5 * train max_drawdown_pct`` when train
         metrics are known (no drawdown explosion);
      3. ``profit_factor >= 0.6 * train profit_factor`` when train metrics
         are known.

    Train metrics are the latest ``fast_gate`` run metrics in the store,
    falling back to ``train_metrics[candidate_id]`` when provided.
    """
    timerange = str(ctx.miner_cfg["validation_timerange"])
    filters = ctx.miner_cfg["hard_filters"]
    log_dir = ctx.root / "agent_logs"
    statuses: dict[int, str] = {}

    for candidate_id in candidate_ids:
        row = _candidate_row(ctx, candidate_id)
        if row is None:
            statuses[candidate_id] = "failed: unknown candidate_id"
            continue
        class_name = row["class_name"]
        args = backtest_args(ctx, class_name, timerange)
        since_ts = time.time()
        result = ctx.runner.run(
            args, log_dir=log_dir, log_name=f"validation_{class_name}.log"
        )

        if result.exit_code != 0:
            note = f"runner exit_code={result.exit_code}"
            record_stage_run(ctx, candidate_id, STAGE_VALIDATION, args, result, notes=note)
            ctx.store.set_stage_status(candidate_id, STAGE_VALIDATION, "failed", note=note)
            statuses[candidate_id] = f"failed: {note}"
            continue

        artifact = find_latest_artifact(ctx, since_ts)
        if artifact is None:
            note = "no backtest artifact found after successful run"
            record_stage_run(ctx, candidate_id, STAGE_VALIDATION, args, result, notes=note)
            ctx.store.set_stage_status(candidate_id, STAGE_VALIDATION, "failed", note=note)
            statuses[candidate_id] = f"failed: {note}"
            continue

        try:
            stats = _select_stats(parse_backtest_result(artifact), class_name)
            metrics = extract_metrics(stats)
        except (OSError, ValueError, KeyError) as exc:
            note = f"unreadable backtest artifact: {exc}"
            record_stage_run(
                ctx, candidate_id, STAGE_VALIDATION, args, result,
                artifact_path=str(artifact), notes=note,
            )
            ctx.store.set_stage_status(candidate_id, STAGE_VALIDATION, "failed", note=note)
            statuses[candidate_id] = f"failed: {note}"
            continue

        record_stage_run(
            ctx, candidate_id, STAGE_VALIDATION, args, result,
            artifact_path=str(artifact), metrics=asdict(metrics),
        )

        reason = _rejection_reason(ctx, candidate_id, metrics, filters, train_metrics)
        if reason is None:
            ctx.store.set_stage_status(candidate_id, STAGE_VALIDATION, "passed")
            statuses[candidate_id] = "passed"
        else:
            ctx.store.set_stage_status(
                candidate_id, STAGE_VALIDATION, "rejected", note=reason
            )
            statuses[candidate_id] = f"rejected: {reason}"

    return statuses


def refine_pipeline(
    ctx: PipelineContext, candidate_ids: Sequence[int], *, epochs: int | None = None
) -> dict[str, dict[int, str]]:
    """Run hyperopt then validation; only hyperopt-passed ids are validated."""
    hyperopt = run_hyperopt(ctx, candidate_ids, epochs=epochs)
    passed = [cid for cid in candidate_ids if hyperopt.get(cid) == "passed"]
    validation = run_validation(ctx, passed)
    return {"hyperopt": hyperopt, "validation": validation}


def _candidate_row(ctx: PipelineContext, candidate_id: int) -> dict | None:
    """Candidate row dict, or None when the id is unknown."""
    try:
        return ctx.store.get_candidate(candidate_id)
    except KeyError:
        return None


def _genome_from_row(row: dict) -> StrategyGenome:
    """Rebuild the StrategyGenome stored in a candidate row's ``genome_json``."""
    data = json.loads(row["genome_json"])
    known = {f.name for f in fields(StrategyGenome)}
    return StrategyGenome(**{k: v for k, v in data.items() if k in known})


def _store_path(ctx: PipelineContext, path: Path) -> str:
    """Path rendered relative to the worktree root (the ``py_path`` convention)."""
    try:
        return str(path.relative_to(ctx.root))
    except ValueError:
        return str(path)


def _select_stats(parsed: dict[str, StrategyStats], class_name: str) -> StrategyStats:
    """Pick the StrategyStats for ``class_name`` (fallback: the only/first strategy)."""
    if class_name in parsed:
        return parsed[class_name]
    if len(parsed) == 1:
        return next(iter(parsed.values()))
    return parsed[sorted(parsed)[0]]


def _train_metrics_for(
    ctx: PipelineContext,
    candidate_id: int,
    train_metrics: Mapping[int, dict] | None,
) -> dict | None:
    """Latest stored fast_gate metrics for the candidate, else the arg fallback."""
    for run in reversed(ctx.store.runs_for(candidate_id, "fast_gate")):
        if run.get("metrics"):
            return run["metrics"]
    if train_metrics:
        found = train_metrics.get(candidate_id)
        if found:
            return found
    return None


def _rejection_reason(
    ctx: PipelineContext,
    candidate_id: int,
    metrics: fitness.Metrics,
    filters: dict,
    train_metrics: Mapping[int, dict] | None,
) -> str | None:
    """First failing gate's reason, or None when all gates hold."""
    if not fitness.passes_hard_filters(metrics, **filters):
        return "failed hard filters"
    train = _train_metrics_for(ctx, candidate_id, train_metrics)
    if train is None:
        return None
    train_dd = train.get("max_drawdown_pct")
    if train_dd is not None and metrics.max_drawdown_pct > 1.5 * float(train_dd):
        return (
            f"drawdown explosion: validation {metrics.max_drawdown_pct:.2f}% "
            f"> 1.5x train {float(train_dd):.2f}%"
        )
    train_pf = train.get("profit_factor")
    if train_pf is not None and metrics.profit_factor < 0.6 * float(train_pf):
        return (
            f"profit factor collapse: validation {metrics.profit_factor:.2f} "
            f"< 0.6x train {float(train_pf):.2f}"
        )
    return None
