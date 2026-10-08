"""Fast gate — pipeline stage 3 (WORKFLOW.md).

Generates candidate genomes, compiles each into a strategy file, backtests the
TRAIN timerange for every candidate, applies the fast-gate screen filters
(``fast_gate_filters`` when configured, else ``hard_filters``) and shortlists
the top survivors. Runs without docker in tests via any object implementing the
``Runner`` protocol (see tests/test_pipeline_fast_gate.py::FakeRunner).
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass

from strategy_miner import fitness
from strategy_miner.compiler import write_strategy
from strategy_miner.generator import GenomeGenerator
from strategy_miner.results import StrategyStats, extract_metrics, parse_backtest_result

from . import (
    PipelineContext,
    backtest_args,
    find_latest_artifact,
    record_stage_run,
    sync_ft_config,
)

STAGE = "fast_gate"


@dataclass(frozen=True)
class FastGateResult:
    """Outcome of one fast-gate pass.

    ``scores``/``artifact_paths`` cover ALL survivors (candidates that passed the
    fast-gate filters), not only the shortlisted ones; ``rejected`` counts
    candidates that failed the fast-gate filters. ``shortlisted`` holds the
    chosen candidate ids.
    """

    generated: int
    backtested: int
    failed_runs: int
    rejected: int
    shortlisted: tuple[int, ...]
    scores: dict[int, float]
    artifact_paths: dict[int, str]


def _select_stats(parsed: dict[str, StrategyStats], class_name: str) -> StrategyStats:
    """Pick the StrategyStats for ``class_name`` (fallback: the only/first strategy)."""
    if class_name in parsed:
        return parsed[class_name]
    if len(parsed) == 1:
        return next(iter(parsed.values()))
    return parsed[sorted(parsed)[0]]


def run_fast_gate(ctx: PipelineContext, *, count: int | None = None) -> FastGateResult:
    """Run the fast-gate stage (WORKFLOW.md stage 3) over ``count`` new candidates."""
    cfg = ctx.miner_cfg
    n = int(count) if count is not None else int(cfg["candidate_count"])

    # Stage 1+2: generate genomes and compile each into a strategy file.
    generator = GenomeGenerator(
        seed=int(cfg["seed"]),
        timeframe=str(cfg["timeframe"]),
        can_short=bool(cfg["can_short"]),
    )
    candidates: list[tuple[int, str]] = []  # (candidate_id, class_name)
    for genome in generator.generate_many(n):
        py_path = write_strategy(genome, ctx.generated_dir)
        # Convention: py_path is stored relative to the worktree root (os-native
        # separators), matching orchestrator.generate_batch.
        candidate_id = ctx.store.upsert_candidate(genome, str(py_path.relative_to(ctx.root)))
        candidates.append((candidate_id, genome.class_name))

    # The runtime freqtrade config is written once — it is not per candidate.
    sync_ft_config(ctx)

    log_dir = ctx.root / "agent_logs"
    timerange = str(cfg["train_timerange"])
    # Pre-hyperopt screen bar (``fast_gate_filters``, DECISIONS D-017): a loose
    # filter that only drops catastrophes — ranking picks the shortlist; the
    # production ``hard_filters`` bar applies after refinement
    # (validation/final) instead of before it. Falls back to ``hard_filters``
    # when the screen key is absent.
    filters = cfg.get("fast_gate_filters", cfg["hard_filters"])

    backtested = 0
    failed_runs = 0
    rejected = 0
    scores: dict[int, float] = {}
    artifact_paths: dict[int, str] = {}
    survivors: list[tuple[int, float]] = []  # (candidate_id, fitness score)

    for candidate_id, class_name in candidates:
        args = backtest_args(ctx, class_name, timerange)
        since_ts = time.time()
        result = ctx.runner.run(
            args, log_dir=log_dir, log_name=f"fast_gate_{class_name}.log"
        )

        if result.exit_code != 0:
            failed_runs += 1
            note = f"runner exit_code={result.exit_code}"
            record_stage_run(ctx, candidate_id, STAGE, args, result, notes=note)
            ctx.store.set_stage_status(candidate_id, STAGE, "failed", note=note)
            continue

        artifact = find_latest_artifact(ctx, since_ts)
        if artifact is None:
            failed_runs += 1
            note = "no backtest artifact found after successful run"
            record_stage_run(ctx, candidate_id, STAGE, args, result, notes=note)
            ctx.store.set_stage_status(candidate_id, STAGE, "failed", note=note)
            continue

        try:
            stats = _select_stats(parse_backtest_result(artifact), class_name)
            metrics = extract_metrics(stats)
        except (OSError, ValueError, KeyError) as exc:
            failed_runs += 1
            note = f"unreadable backtest artifact: {exc}"
            record_stage_run(
                ctx, candidate_id, STAGE, args, result,
                artifact_path=str(artifact), notes=note,
            )
            ctx.store.set_stage_status(candidate_id, STAGE, "failed", note=note)
            continue

        backtested += 1
        score = fitness.score(metrics)
        record_stage_run(
            ctx, candidate_id, STAGE, args, result,
            artifact_path=str(artifact), metrics=asdict(metrics),
        )
        if not fitness.passes_hard_filters(metrics, **filters):
            rejected += 1
            ctx.store.set_stage_status(candidate_id, STAGE, "rejected",
                                       note="failed fast-gate filters")
            continue
        scores[candidate_id] = score
        artifact_paths[candidate_id] = str(artifact)
        survivors.append((candidate_id, score))
        ctx.store.set_stage_status(candidate_id, STAGE, "passed")

    # Shortlist: rank survivors by fitness score (desc, id asc for ties) and take top k.
    selection = cfg["selection"]
    k = max(
        int(math.ceil(float(selection["top_fraction"]) * len(candidates))),
        int(selection["min_candidates"]),
    )
    survivors.sort(key=lambda item: (-item[1], item[0]))
    shortlisted = tuple(candidate_id for candidate_id, _ in survivors[:k])
    for candidate_id, _ in survivors[k:]:
        ctx.store.set_stage_status(candidate_id, STAGE, "rejected",
                                   note="passed fast-gate filters but outside top shortlist")
    for candidate_id in shortlisted:
        ctx.store.set_stage_status(candidate_id, STAGE, "shortlisted")

    return FastGateResult(
        generated=len(candidates),
        backtested=backtested,
        failed_runs=failed_runs,
        rejected=rejected,
        shortlisted=shortlisted,
        scores=scores,
        artifact_paths=artifact_paths,
    )
