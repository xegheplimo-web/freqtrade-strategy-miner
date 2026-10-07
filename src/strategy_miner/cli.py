"""Command-line entry point wiring every pipeline stage (T-205).

Commands (names frozen by ``.orchestrator/INTERFACES.md`` section 6)::

    generate data-download data-audit fast-gate hyperopt validate bias
    walk-forward robust final report status run

Stage chaining for the id-driven commands (``--ids`` overrides the derivation):

- ``hyperopt``: ``--ids`` or ``store.shortlist()`` (fast_gate ``shortlisted``).
- ``validate``: ``--ids`` or candidates with ``hyperopt == "passed"``.
- ``bias``: ``--ids`` or candidates with ``validation == "passed"``.
- ``walk-forward``: ``--ids`` or candidates with ``bias == "passed"``.
- ``robust``: ``--ids`` or candidates with ``walk_forward == "passed"``.
- ``final``: ``--ids`` or candidates with ``robustness == "passed"``.

Exit-code convention (also documented in ``docs/pipeline.md``): every command
prints a ``json.dumps(..., default=str, ensure_ascii=False)`` summary and
returns ``0`` on success. A command returns ``1`` when a stage reports a
runner/artifact problem, i.e. any ``"failed..."`` status (``"failed: ..."``,
``"failed"``) or, for ``fast-gate``, ``failed_runs > 0``. ``"rejected"`` (a
gate did not hold) and ``"skipped"`` are legitimate gate outcomes and keep the
exit code at ``0``. ``data-audit`` returns ``1`` when the audit is not ok
(inside ``run`` it is warn-only and never stops the sequence). ``2`` signals a
usage/config error (e.g. no usable timerange for ``data-download``).

``main(argv, ctx_factory=...)`` is testable: ``ctx_factory`` defaults to
``pipeline.make_context`` and is called as ``ctx_factory(root,
config_path=config)``; tests inject a fake returning a stub context.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from strategy_miner import data, orchestrator
from strategy_miner.pipeline import (
    FT_CONFIG_NAME,
    PipelineContext,
    make_context,
    sync_ft_config,
)
from strategy_miner.pipeline.checks import (
    run_bias_checks,
    run_final,
    run_walk_forward,
)
from strategy_miner.pipeline.fast_gate import run_fast_gate
from strategy_miner.pipeline.refine import run_hyperopt, run_validation
from strategy_miner.pipeline.robustness import run_robustness
from strategy_miner.runner import CONTAINER_USERDATA, DockerRunner
from strategy_miner.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]

STAGES: tuple[str, ...] = (
    "fast_gate",
    "hyperopt",
    "validation",
    "bias",
    "walk_forward",
    "robustness",
    "final_test",
)

ContextFactory = Callable[..., PipelineContext]


# --------------------------------------------------------------------------- helpers


def _emit(payload: Any) -> None:
    """Print a JSON summary line (keys/values stringified when needed)."""
    print(json.dumps(payload, default=str, ensure_ascii=False))


def _has_failed(statuses: Mapping[Any, str]) -> bool:
    """True when any status reports a runner/artifact failure (``failed...``)."""
    return any(str(status).startswith("failed") for status in statuses.values())


def _ids_with_status(ctx: PipelineContext, stage: str, status: str) -> list[int]:
    """Ids of candidates whose stored ``stage`` status equals ``status``."""
    ids: list[int] = []
    for row in ctx.store.list_candidates():
        if ctx.store.stage_status(row["id"], stage) == status:
            ids.append(row["id"])
    return ids


def _cfg_pairs(ctx: PipelineContext) -> tuple[list[str], list[str], str, str]:
    """Return ``(pairs, timeframes, exchange, trading_mode)`` from the miner cfg."""
    cfg = ctx.miner_cfg
    pairs = list(cfg.get("pairs", []))
    timeframes = [str(cfg.get("timeframe", "5m"))]
    exchange = str(cfg.get("exchange", "binance"))
    trading_mode = str(cfg.get("trading_mode", "futures"))
    return pairs, timeframes, exchange, trading_mode


def _full_timerange(cfg: Mapping[str, Any], override: str | None) -> str | None:
    """Download timerange: ``--timerange`` or ``train_start-final_end`` from cfg."""
    if override:
        return override
    train = str(cfg.get("train_timerange", ""))
    final = str(cfg.get("final_test_timerange", ""))
    if "-" in train and "-" in final:
        return f"{train.split('-')[0]}-{final.split('-')[1]}"
    return None


def _audit_payload(ctx: PipelineContext) -> dict[str, Any]:
    """Run the data audit and return its JSON-serialisable summary."""
    pairs, timeframes, exchange, trading_mode = _cfg_pairs(ctx)
    audit = data.audit_data(
        ctx.user_data / "data",
        pairs,
        timeframes,
        exchange=exchange,
        trading_mode=trading_mode,
    )
    return {
        "ok": audit.ok,
        "issues": list(audit.issues),
        "pairs": [
            {
                "pair": item.pair,
                "timeframe": item.timeframe,
                "rows": item.rows,
                "missing": item.missing,
            }
            for item in audit.pairs
        ],
    }


def _make_context(
    ctx_factory: ContextFactory, root: Path, config: Path
) -> PipelineContext:
    """Build the pipeline context, tolerating a bare root (smoke tests).

    When ``config`` does not exist (e.g. ``status`` run against an empty tmp
    root) a minimal context with an empty miner cfg is built instead of
    crashing, so read-only commands keep working.
    """
    try:
        return ctx_factory(root, config_path=config)
    except FileNotFoundError:
        user_data = root / "freqtrade" / "user_data"
        generated_dir = user_data / "strategies" / "generated"
        results_dir = user_data / "backtest_results"
        generated_dir.mkdir(parents=True, exist_ok=True)
        results_dir.mkdir(parents=True, exist_ok=True)
        return PipelineContext(
            root=root,
            miner_cfg={},
            runner=DockerRunner(user_data),
            store=Store(root / "output" / "miner.db"),
            user_data=user_data,
            generated_dir=generated_dir,
            results_dir=results_dir,
            ft_config_container=f"{CONTAINER_USERDATA}/{FT_CONFIG_NAME}",
            generated_dir_container=f"{CONTAINER_USERDATA}/strategies/generated",
            results_dir_container=f"{CONTAINER_USERDATA}/backtest_results",
        )


def _resolve_ids(args: argparse.Namespace, derived: list[int]) -> list[int]:
    """CLI ``--ids`` override, falling back to the store-derived list."""
    if args.ids:
        return list(args.ids)
    return derived


def _empty_note(stage: str) -> dict[str, Any]:
    """JSON payload used when a stage has no candidates to process."""
    return {stage: {}, "note": "no candidates (empty id list); stage skipped"}


# --------------------------------------------------------------------------- commands


def cmd_generate(root: Path, args: argparse.Namespace) -> int:
    """Generate strategies + manifest via ``orchestrator.generate_batch``."""
    (root / "freqtrade" / "user_data" / "strategies" / "generated").mkdir(
        parents=True, exist_ok=True
    )
    result = orchestrator.generate_batch(root, count=args.count)
    _emit(result)
    return 0


def cmd_data_download(
    ctx: PipelineContext, args: argparse.Namespace, _root: Path
) -> int:
    """Download candle data for ``train_start-final_end`` (or ``--timerange``)."""
    timerange = _full_timerange(ctx.miner_cfg, args.timerange)
    if timerange is None:
        _emit({"error": "no timerange: pass --timerange or set train/final ranges"})
        return 2
    pairs, timeframes, exchange, _trading_mode = _cfg_pairs(ctx)
    sync_ft_config(ctx)
    argv = data.download_args(
        ctx.ft_config_container, pairs, timeframes, timerange, exchange=exchange
    )
    result = ctx.runner.run(
        argv, log_dir=ctx.root / "agent_logs", log_name="data_download.log"
    )
    _emit(
        {
            "exit_code": result.exit_code,
            "log": str(result.log_path),
            "argv": list(result.argv),
            "timerange": timerange,
        }
    )
    return 0 if result.exit_code == 0 else 1


def cmd_data_audit(
    ctx: PipelineContext, _args: argparse.Namespace, _root: Path
) -> int:
    """Audit feather data files and print ``ok`` + issues."""
    payload = _audit_payload(ctx)
    _emit(payload)
    return 0 if payload["ok"] else 1


def cmd_fast_gate(
    ctx: PipelineContext, args: argparse.Namespace, _root: Path
) -> int:
    """Run the fast-gate stage for ``--count`` new candidates."""
    result = run_fast_gate(ctx, count=args.count)
    _emit(asdict(result))
    return 1 if result.failed_runs > 0 else 0


def cmd_hyperopt(
    ctx: PipelineContext, args: argparse.Namespace, _root: Path
) -> int:
    """Run hyperopt for ``--ids`` or the store shortlist."""
    ids = _resolve_ids(args, ctx.store.shortlist())
    if not ids:
        _emit(_empty_note("hyperopt"))
        return 0
    statuses = run_hyperopt(ctx, ids, epochs=args.epochs)
    _emit({"hyperopt": statuses})
    return 1 if _has_failed(statuses) else 0


def cmd_validate(
    ctx: PipelineContext, args: argparse.Namespace, _root: Path
) -> int:
    """Run validation for ``--ids`` or hyperopt-passed candidates."""
    ids = _resolve_ids(args, _ids_with_status(ctx, "hyperopt", "passed"))
    if not ids:
        _emit(_empty_note("validation"))
        return 0
    statuses = run_validation(ctx, ids)
    _emit({"validation": statuses})
    return 1 if _has_failed(statuses) else 0


def cmd_bias(ctx: PipelineContext, args: argparse.Namespace, _root: Path) -> int:
    """Run bias checks for ``--ids`` or validation-passed candidates."""
    ids = _resolve_ids(args, _ids_with_status(ctx, "validation", "passed"))
    if not ids:
        _emit(_empty_note("bias"))
        return 0
    statuses = run_bias_checks(ctx, ids)
    _emit({"bias": statuses})
    return 1 if _has_failed(statuses) else 0


def cmd_walk_forward(
    ctx: PipelineContext, args: argparse.Namespace, _root: Path
) -> int:
    """Run walk-forward for ``--ids`` or bias-passed candidates."""
    ids = _resolve_ids(args, _ids_with_status(ctx, "bias", "passed"))
    if not ids:
        _emit(_empty_note("walk_forward"))
        return 0
    statuses = run_walk_forward(ctx, ids, max_folds=args.max_folds)
    _emit({"walk_forward": statuses})
    return 1 if _has_failed(statuses) else 0


def cmd_robust(ctx: PipelineContext, args: argparse.Namespace, _root: Path) -> int:
    """Run robustness for ``--ids`` or walk-forward-passed candidates."""
    ids = _resolve_ids(args, _ids_with_status(ctx, "walk_forward", "passed"))
    if not ids:
        _emit(_empty_note("robustness"))
        return 0
    statuses = run_robustness(ctx, ids)
    _emit({"robustness": statuses})
    return 1 if _has_failed(statuses) else 0


def cmd_final(ctx: PipelineContext, args: argparse.Namespace, _root: Path) -> int:
    """Run the one-shot final test for ``--ids`` or robustness-passed ids."""
    ids = _resolve_ids(args, _ids_with_status(ctx, "robustness", "passed"))
    if not ids:
        _emit(_empty_note("final_test"))
        return 0
    statuses = run_final(ctx, ids, force=args.force)
    _emit({"final_test": statuses})
    return 1 if _has_failed(statuses) else 0


def cmd_report(ctx: PipelineContext, _args: argparse.Namespace, _root: Path) -> int:
    """Write ``output/report.md`` (candidates x stage statuses + train metrics)."""
    header = (
        "| class | strategy_id | fast_gate | hyperopt | validation | bias | "
        "walk_forward | robustness | final_test | train metrics |"
    )
    separator = "|---|---|---|---|---|---|---|---|---|---|"
    lines = ["# Strategy Miner Report", "", header, separator]
    for row in ctx.store.list_candidates():
        candidate_id = row["id"]
        cells = [
            str(ctx.store.stage_status(candidate_id, stage) or "-")
            for stage in STAGES
        ]
        metrics_cell = "-"
        for run in reversed(ctx.store.runs_for(candidate_id, "fast_gate")):
            metrics = run.get("metrics")
            if metrics:
                metrics_cell = (
                    f"trades={metrics.get('trades', '-')} "
                    f"pf={metrics.get('profit_factor', '-')} "
                    f"ret%={metrics.get('total_return_pct', '-')} "
                    f"dd%={metrics.get('max_drawdown_pct', '-')}"
                )
                break
        strategy_id = row.get("strategy_id")
        lines.append(
            f"| {row['class_name']} | {strategy_id if strategy_id is not None else '-'} "
            f"| {' | '.join(cells)} | {metrics_cell} |"
        )
    report_path = ctx.root / "output" / "report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _emit({"report": str(report_path), "candidates": len(ctx.store.list_candidates())})
    return 0


def cmd_status(ctx: PipelineContext, _args: argparse.Namespace, _root: Path) -> int:
    """Print candidate counts by stage/status from the store."""
    counts: dict[str, dict[str, int]] = {stage: {} for stage in STAGES}
    rows = ctx.store.list_candidates()
    for row in rows:
        for stage in STAGES:
            status = ctx.store.stage_status(row["id"], stage)
            if status is None:
                continue
            counts[stage][status] = counts[stage].get(status, 0) + 1
    _emit({"candidates": len(rows), "counts": counts})
    return 0


def cmd_run(ctx: PipelineContext, args: argparse.Namespace, _root: Path) -> int:
    """E2E sequence: audit (warn-only) -> fast-gate -> ... -> final.

    Stops at the first stage whose id list is empty and prints a per-stage
    summary mapping at the end.
    """
    code = 0
    summary: dict[str, Any] = {}

    audit = _audit_payload(ctx)
    summary["data-audit"] = audit
    _emit({"stage": "data-audit", **audit})

    gated = run_fast_gate(ctx, count=args.count)
    summary["fast-gate"] = asdict(gated)
    _emit({"stage": "fast-gate", **summary["fast-gate"]})
    if gated.failed_runs > 0:
        code = 1

    stages: Sequence[tuple[str, Callable[[list[int]], dict[int, str]]]] = (
        ("hyperopt", lambda ids: run_hyperopt(ctx, ids, epochs=args.epochs)),
        ("validate", lambda ids: run_validation(ctx, ids)),
        ("bias", lambda ids: run_bias_checks(ctx, ids)),
        (
            "walk-forward",
            lambda ids: run_walk_forward(ctx, ids, max_folds=args.max_folds),
        ),
        ("robust", lambda ids: run_robustness(ctx, ids)),
        ("final", lambda ids: run_final(ctx, ids, force=args.force)),
    )
    ids: list[int] = list(ctx.store.shortlist())
    for name, runner_fn in stages:
        if not ids:
            summary[name] = {"note": "stopped: empty id list"}
            break
        statuses = runner_fn(ids)
        summary[name] = {str(key): value for key, value in statuses.items()}
        _emit({"stage": name, **summary[name]})
        if _has_failed(statuses):
            code = 1
        ids = [key for key, value in statuses.items() if value == "passed"]

    _emit({"run_summary": summary})
    return code


# --------------------------------------------------------------------------- parser


def _add_ids(parser: argparse.ArgumentParser) -> None:
    """Attach the shared ``--ids`` option (candidate ids, ints)."""
    parser.add_argument("--ids", nargs="*", type=int, default=None, metavar="ID",
                        help="candidate ids (default: derived from the store)")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser (global ``--root``/``--config`` + 13 commands)."""
    parser = argparse.ArgumentParser(
        prog="miner",
        description="Freqtrade strategy miner: generate, gate and validate strategies.",
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT,
                        help="repo/worktree root (default: repo root)")
    parser.add_argument("--config", type=Path, default=None,
                        help="miner config path (default: <root>/config/miner.json)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    p_generate = sub.add_parser("generate", help="generate strategies + manifest")
    p_generate.add_argument("--count", type=int, default=None,
                            help="candidates to generate (default: config candidate_count)")

    p_download = sub.add_parser("data-download", help="download OHLCV data via freqtrade")
    p_download.add_argument("--timerange", type=str, default=None,
                            help="freqtrade timerange (default: train_start-final_end)")

    sub.add_parser("data-audit", help="audit local feather data files")

    p_gate = sub.add_parser("fast-gate", help="backtest TRAIN + shortlist survivors")
    p_gate.add_argument("--count", type=int, default=None,
                        help="candidates to generate (default: config candidate_count)")

    p_hyperopt = sub.add_parser("hyperopt", help="hyperopt TRAIN for shortlisted ids")
    _add_ids(p_hyperopt)
    p_hyperopt.add_argument("--epochs", type=int, default=None,
                            help="hyperopt epochs (default: config or 300)")

    p_validate = sub.add_parser("validate", help="backtest VALIDATION timerange")
    _add_ids(p_validate)

    p_bias = sub.add_parser("bias", help="lookahead + recursive bias checks")
    _add_ids(p_bias)

    p_wf = sub.add_parser("walk-forward", help="rolling out-of-sample folds")
    _add_ids(p_wf)
    p_wf.add_argument("--max-folds", type=int, default=None,
                      help="cap the number of folds (default: all windows)")

    p_robust = sub.add_parser("robust", help="fee/perturb/drop-top robustness")
    _add_ids(p_robust)

    p_final = sub.add_parser("final", help="one-shot FINAL_TEST + champions")
    _add_ids(p_final)
    p_final.add_argument("--force", action="store_true",
                         help="re-test candidates that already have a final_test run")

    sub.add_parser("report", help="write output/report.md")
    sub.add_parser("status", help="print store counts by stage/status")

    p_run = sub.add_parser("run", help="full E2E sequence audit -> final")
    p_run.add_argument("--count", type=int, default=None,
                       help="candidates to generate (default: config candidate_count)")
    p_run.add_argument("--epochs", type=int, default=None,
                       help="hyperopt epochs (default: config or 300)")
    p_run.add_argument("--max-folds", type=int, default=None,
                       help="cap the walk-forward folds (default: all windows)")
    p_run.add_argument("--force", action="store_true",
                       help="re-test candidates that already have a final_test run")
    return parser


_HANDLERS: dict[str, Callable[[PipelineContext, argparse.Namespace, Path], int]] = {
    "data-download": cmd_data_download,
    "data-audit": cmd_data_audit,
    "fast-gate": cmd_fast_gate,
    "hyperopt": cmd_hyperopt,
    "validate": cmd_validate,
    "bias": cmd_bias,
    "walk-forward": cmd_walk_forward,
    "robust": cmd_robust,
    "final": cmd_final,
    "report": cmd_report,
    "status": cmd_status,
    "run": cmd_run,
}


def main(
    argv: Sequence[str] | None = None,
    *,
    ctx_factory: ContextFactory | None = None,
) -> int:
    """CLI entry point; returns the process exit code."""
    args = build_parser().parse_args(argv)
    root = Path(args.root)
    config = Path(args.config) if args.config is not None else root / "config" / "miner.json"
    if args.command == "generate":
        return cmd_generate(root, args)
    factory: ContextFactory = make_context if ctx_factory is None else ctx_factory
    ctx = _make_context(factory, root, config)
    return _HANDLERS[args.command](ctx, args, root)
