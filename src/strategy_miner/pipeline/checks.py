"""Pipeline checks — bias (stage 6), walk-forward (stage 7) and final gate (stage 9).

This module maps onto ``docs/WORKFLOW.md``:

- Stage 6 "Bias checks" — :func:`run_bias_checks` runs freqtrade ``lookahead-analysis``
  and ``recursive-analysis`` per surviving candidate. The exact argv builders are
  :func:`lookahead_args` / :func:`recursive_args`; the machine-readable parsers are
  :func:`parse_lookahead_csv` / :func:`parse_recursive_stdout`. Any lookahead bias is a
  hard reject.
- Stage 7 "Walk-forward" — :func:`run_walk_forward` rolls a train/test window over the
  TRAIN->VALIDATION span and aggregates the out-of-sample folds (median profit factor +
  positive-expectancy fraction). Window helpers: :func:`add_months` /
  :func:`walk_forward_windows`.
- Stage 9 "Final Test" — :func:`run_final` runs the one-shot FINAL_TEST backtest on frozen
  candidates and copies the champion artifacts into ``output/champions/<Class>/``.

Every stage runs without docker in tests: it accepts any object implementing the
``Runner`` protocol (INTERFACES.md section 1). No third-party dependencies are used.
"""

from __future__ import annotations

import calendar
import csv
import json
import re
import shutil
import statistics
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from strategy_miner import fitness
from strategy_miner.results import StrategyStats, extract_metrics, parse_backtest_result
from strategy_miner.runner import CONTAINER_USERDATA

from . import PipelineContext, backtest_args, find_latest_artifact, record_stage_run

BIAS_STAGE = "bias"
WF_STAGE = "walk_forward"
FINAL_STAGE = "final_test"

# Container sub-directory holding the lookahead CSV exports.
ANALYSIS_SUBDIR = "analysis"

# A percentage cell, e.g. "0.000%" or "12.500%".
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)%")
# CSI escape sequences (defensive: the Runner already strips ANSI).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _select_stats(parsed: dict[str, StrategyStats], class_name: str) -> StrategyStats:
    """Pick the StrategyStats for ``class_name`` (fallback: the only/first strategy)."""
    if class_name in parsed:
        return parsed[class_name]
    if len(parsed) == 1:
        return next(iter(parsed.values()))
    return parsed[sorted(parsed)[0]]


def _as_bool(value: Any) -> bool:
    """Coerce a CSV cell to bool ("True"/"1"/"yes" -> True)."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


# --------------------------------------------------------------------------- bias stage


def lookahead_args(
    ctx: PipelineContext, class_name: str, timerange: str, csv_container_path: str
) -> list[str]:
    """Exact ``lookahead-analysis`` argv (all container paths)."""
    return [
        "lookahead-analysis",
        "--config", ctx.ft_config_container,
        "--strategy", class_name,
        "--strategy-path", ctx.generated_dir_container,
        "--timerange", timerange,
        "--lookahead-analysis-exportfilename", csv_container_path,
    ]


def recursive_args(ctx: PipelineContext, class_name: str, timerange: str) -> list[str]:
    """Exact ``recursive-analysis`` argv (all container paths)."""
    return [
        "recursive-analysis",
        "--config", ctx.ft_config_container,
        "--strategy", class_name,
        "--strategy-path", ctx.generated_dir_container,
        "--timerange", timerange,
    ]


def parse_lookahead_csv(path: Path | str) -> dict[str, Any] | None:
    """Return the CSV row for the strategy (``has_bias`` coerced to bool).

    ``lookahead-analysis`` does not signal bias via its exit code; the only
    machine-readable signal is the CSV export written with
    ``--lookahead-analysis-exportfilename``. The export contains one row per
    strategy. Returns ``None`` when the file or a data row is missing (which the
    caller treats as an inconclusive test).
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if not row:
                    continue
                row = dict(row)
                row["has_bias"] = _as_bool(row.get("has_bias"))
                return row
    except OSError:
        return None
    return None


def _split_cells(line: str) -> list[str]:
    """Split a table row into cells (pipe-delimited when present, else whitespace)."""
    if "|" in line:
        cells = [cell.strip() for cell in line.split("|")]
    else:
        cells = line.split()
    return [cell for cell in cells if cell]


def parse_recursive_stdout(text: str) -> dict[str, float]:
    """Parse the ``recursive-analysis`` stdout table into ``{indicator: max_pct}``.

    ``recursive-analysis`` has no csv/exit signal: it prints a table whose rows are
    indicators and whose columns are startup-candle percentages. For every line that
    contains at least one ``<number>%`` cell the first cell is taken as the indicator
    name and the maximum percentage on that line is recorded. Table borders, headers
    and any non-table prose are ignored. Returns an empty dict when nothing parses
    (callers record the raw note instead of crashing).
    """
    result: dict[str, float] = {}
    for raw_line in text.splitlines():
        line = _ANSI_RE.sub("", raw_line).strip()
        if not line:
            continue
        percentages = [float(match) for match in _PCT_RE.findall(line)]
        if not percentages:
            continue
        cells = _split_cells(line)
        if not cells:
            continue
        indicator = cells[0]
        if not indicator or set(indicator) <= {"-", "="}:
            continue
        result[indicator] = max(percentages)
    return result


def run_bias_checks(ctx: PipelineContext, candidate_ids: Iterable[int]) -> dict[int, str]:
    """Run the bias stage (WORKFLOW.md stage 6) for each candidate id.

    Both freqtrade commands are executed per candidate and recorded under the
    ``bias`` stage (two runs per candidate). The returned mapping holds the
    descriptive final status per candidate id (``"passed"``, ``"rejected: ..."`` or
    ``"failed: <code>"``); the store receives the matching vocabulary status
    (``passed``/``rejected``/``failed``) with the description in the note.
    """
    cfg = ctx.miner_cfg
    bias_cfg = cfg.get("bias", {}) or {}
    timerange = str(bias_cfg.get("timerange", cfg["validation_timerange"]))
    recursive_max_pct = float(bias_cfg.get("recursive_max_pct", 5.0))

    analysis_dir = ctx.user_data / ANALYSIS_SUBDIR
    analysis_dir.mkdir(parents=True, exist_ok=True)
    log_dir = ctx.root / "agent_logs"

    results: dict[int, str] = {}
    for candidate_id in candidate_ids:
        class_name = ctx.store.get_candidate(candidate_id)["class_name"]
        csv_host = analysis_dir / f"lookahead_{class_name}.csv"
        csv_container = f"{CONTAINER_USERDATA}/{ANALYSIS_SUBDIR}/lookahead_{class_name}.csv"

        # --- lookahead-analysis -------------------------------------------------
        la_args = lookahead_args(ctx, class_name, timerange, csv_container)
        la_result = ctx.runner.run(
            la_args, log_dir=log_dir, log_name=f"bias_lookahead_{class_name}.log"
        )
        la_status: str | None
        if la_result.exit_code != 0:
            la_status = f"failed: {la_result.exit_code}"
            la_note = f"lookahead-analysis exit_code={la_result.exit_code}"
        else:
            row = parse_lookahead_csv(csv_host)
            if row is None:
                la_status = "rejected: bias test inconclusive"
                la_note = "lookahead-analysis: no CSV row (too few signals)"
            elif row["has_bias"]:
                la_status = "rejected: lookahead bias"
                la_note = "lookahead-analysis: has_bias=True"
            else:
                la_status = None
                la_note = "lookahead-analysis: clean"
        record_stage_run(
            ctx, candidate_id, BIAS_STAGE, la_args, la_result,
            artifact_path=str(csv_host) if csv_host.is_file() else None,
            notes=la_note,
        )

        # --- recursive-analysis -------------------------------------------------
        rec_args = recursive_args(ctx, class_name, timerange)
        rec_result = ctx.runner.run(
            rec_args, log_dir=log_dir, log_name=f"bias_recursive_{class_name}.log"
        )
        rec_status: str | None
        rec_pcts: dict[str, float] = {}
        if rec_result.exit_code != 0:
            rec_status = f"failed: {rec_result.exit_code}"
            rec_note = f"recursive-analysis exit_code={rec_result.exit_code}"
        else:
            rec_pcts = parse_recursive_stdout(rec_result.stdout)
            worst_indicator = ""
            worst_pct = 0.0
            for indicator, pct in rec_pcts.items():
                if pct > worst_pct:
                    worst_indicator, worst_pct = indicator, pct
            if rec_pcts and worst_pct >= recursive_max_pct:
                rec_status = f"rejected: recursive {worst_indicator} {worst_pct}%"
                rec_note = f"recursive-analysis max {worst_indicator}={worst_pct}%"
            else:
                rec_status = None
                rec_note = (
                    f"recursive-analysis pcts={rec_pcts}"
                    if rec_pcts
                    else f"recursive-analysis: unparsed output: {rec_result.stdout.strip()[:200]}"
                )
        record_stage_run(
            ctx, candidate_id, BIAS_STAGE, rec_args, rec_result,
            metrics=rec_pcts or None, notes=rec_note,
        )

        # --- combine (failed > rejected > passed) -------------------------------
        final = _combine_bias_status(la_status, rec_status)
        status_kind = "passed" if final == "passed" else final.split(":", 1)[0]
        ctx.store.set_stage_status(candidate_id, BIAS_STAGE, status_kind, note=final)
        results[candidate_id] = final
    return results


def _combine_bias_status(la_status: str | None, rec_status: str | None) -> str:
    """Merge the lookahead/recursive outcomes (failed > rejected > passed)."""
    for status in (la_status, rec_status):
        if status is not None and status.startswith("failed"):
            return status
    for status in (la_status, rec_status):
        if status is not None:
            return status
    return "passed"


# --------------------------------------------------------------- walk-forward stage


def add_months(ymd: str, n: int) -> str:
    """Add ``n`` calendar months to a ``YYYYMMDD`` date (day clamped to month end)."""
    year = int(ymd[0:4])
    month = int(ymd[4:6])
    day = int(ymd[6:8])
    total = year * 12 + (month - 1) + n
    new_year, new_month0 = divmod(total, 12)
    new_month = new_month0 + 1
    last_day = calendar.monthrange(new_year, new_month)[1]
    return f"{new_year:04d}{new_month:02d}{min(day, last_day):02d}"


def walk_forward_windows(
    train_start: str,
    end: str,
    *,
    train_months: int = 12,
    test_months: int = 3,
    step_months: int = 3,
) -> list[tuple[str, str, str]]:
    """Return rolling (train_start, test_start, test_end) folds as ``YYYYMMDD``.

    Fold ``k`` tests ``[train_start + train_months + k*step_months, + test_months]``
    and folds are emitted while ``test_end <= end``. Returns an empty list when the
    span cannot fit a single fold.
    """
    folds: list[tuple[str, str, str]] = []
    k = 0
    while True:
        test_start = add_months(train_start, train_months + k * step_months)
        test_end = add_months(test_start, test_months)
        if test_end > end:
            break
        folds.append((train_start, test_start, test_end))
        k += 1
    return folds


def run_walk_forward(
    ctx: PipelineContext,
    candidate_ids: Iterable[int],
    *,
    train_months: int = 12,
    test_months: int = 3,
    step_months: int = 3,
    max_folds: int | None = None,
) -> dict[int, str]:
    """Run the walk-forward stage (WORKFLOW.md stage 7) — rolling out-of-sample.

    The span is ``train_timerange`` start -> ``validation_timerange`` end. Per fold
    only the TEST window is backtested; the train window is *context* — parameters
    are NOT re-optimized per fold (a deliberate deviation from classic walk-forward
    optimization, because the pipeline keeps a single frozen parameter set after
    stage 4/5). Each fold run is recorded under the ``walk_forward`` stage with the
    fold window as its note.

    Gates: ``median(test profit_factor) >= 1.0`` AND the fraction of successful folds
    with ``expectancy > 0`` ``>= 0.6`` -> ``"passed"``. Fold failures (non-zero exit
    or missing artifact) are skipped; when more than 30% of folds fail the candidate
    is ``"failed"``.
    """
    cfg = ctx.miner_cfg
    train_start = str(cfg["train_timerange"]).split("-")[0]
    end = str(cfg["validation_timerange"]).split("-")[1]

    folds = walk_forward_windows(
        train_start, end,
        train_months=train_months, test_months=test_months, step_months=step_months,
    )
    if max_folds is not None:
        folds = folds[:max_folds]

    log_dir = ctx.root / "agent_logs"
    results: dict[int, str] = {}
    for candidate_id in candidate_ids:
        class_name = ctx.store.get_candidate(candidate_id)["class_name"]

        if not folds:
            status = "failed: no walk-forward windows"
            ctx.store.set_stage_status(candidate_id, WF_STAGE, "failed", note=status)
            results[candidate_id] = status
            continue

        profit_factors: list[float] = []
        positive = 0
        failed_folds = 0
        for _train_start, test_start, test_end in folds:
            timerange = f"{test_start}-{test_end}"
            args = backtest_args(ctx, class_name, timerange)
            since_ts = time.time()
            result = ctx.runner.run(
                args, log_dir=log_dir, log_name=f"walk_forward_{class_name}_{test_start}.log"
            )
            if result.exit_code != 0:
                failed_folds += 1
                record_stage_run(
                    ctx, candidate_id, WF_STAGE, args, result,
                    notes=f"{timerange} exit_code={result.exit_code}",
                )
                continue
            artifact = find_latest_artifact(ctx, since_ts)
            if artifact is None:
                failed_folds += 1
                record_stage_run(
                    ctx, candidate_id, WF_STAGE, args, result,
                    notes=f"{timerange} no artifact",
                )
                continue
            try:
                stats = _select_stats(parse_backtest_result(artifact), class_name)
                metrics = extract_metrics(stats)
            except (OSError, ValueError, KeyError) as exc:
                failed_folds += 1
                record_stage_run(
                    ctx, candidate_id, WF_STAGE, args, result,
                    artifact_path=str(artifact), notes=f"{timerange} unreadable artifact: {exc}",
                )
                continue
            profit_factors.append(metrics.profit_factor)
            if metrics.expectancy > 0:
                positive += 1
            record_stage_run(
                ctx, candidate_id, WF_STAGE, args, result,
                artifact_path=str(artifact), metrics=asdict(metrics), notes=timerange,
            )

        status = _walk_forward_status(folds, profit_factors, positive, failed_folds)
        status_kind = "passed" if status == "passed" else status.split(":", 1)[0]
        ctx.store.set_stage_status(candidate_id, WF_STAGE, status_kind, note=status)
        results[candidate_id] = status
    return results


def _walk_forward_status(
    folds: list[tuple[str, str, str]],
    profit_factors: list[float],
    positive: int,
    failed_folds: int,
) -> str:
    """Apply the walk-forward gates to the aggregated fold metrics."""
    total = len(folds)
    if total and failed_folds / total > 0.3:
        return f"failed: {failed_folds}/{total} folds failed"
    if not profit_factors:
        return "failed: no successful folds"
    median_pf = statistics.median(profit_factors)
    positive_fraction = positive / len(profit_factors)
    if median_pf < 1.0:
        return f"rejected: median profit_factor {median_pf} < 1.0"
    if positive_fraction < 0.6:
        return f"rejected: positive expectancy fraction {positive_fraction} < 0.6"
    return "passed"


# ------------------------------------------------------------------- final gate


def run_final(
    ctx: PipelineContext, candidate_ids: Iterable[int], *, force: bool = False
) -> dict[int, str]:
    """Run the one-shot final gate (WORKFLOW.md stage 9) for each candidate id.

    A candidate must be frozen: its params sidecar ``<generated>/<Class>.json`` has
    to exist, otherwise it is ``"rejected: not frozen (no params file)"``. The test
    is one-shot — a candidate that already has a ``final_test`` run is skipped unless
    ``force`` is set. The FINAL_TEST timerange is backtested once, the summary is
    mapped through the hard filters (plus ``expectancy > 0``) and, on pass, the
    strategy ``.py``, params ``.json`` and result artifact zip are copied into
    ``<root>/output/champions/<Class>/`` together with a ``champion.json`` manifest.
    """
    cfg = ctx.miner_cfg
    timerange = str(cfg["final_test_timerange"])
    filters = cfg["hard_filters"]
    log_dir = ctx.root / "agent_logs"

    results: dict[int, str] = {}
    for candidate_id in candidate_ids:
        class_name = ctx.store.get_candidate(candidate_id)["class_name"]
        py_path = ctx.generated_dir / f"{class_name}.py"
        params_path = ctx.generated_dir / f"{class_name}.json"

        if not params_path.is_file():
            status = "rejected: not frozen (no params file)"
            ctx.store.set_stage_status(candidate_id, FINAL_STAGE, "rejected", note=status)
            results[candidate_id] = status
            continue

        if not force and ctx.store.runs_for(candidate_id, FINAL_STAGE):
            status = "skipped: already tested (use force)"
            ctx.store.set_stage_status(candidate_id, FINAL_STAGE, "skipped", note=status)
            results[candidate_id] = status
            continue

        args = backtest_args(ctx, class_name, timerange)
        since_ts = time.time()
        result = ctx.runner.run(args, log_dir=log_dir, log_name=f"final_{class_name}.log")
        if result.exit_code != 0:
            status = f"failed: {result.exit_code}"
            record_stage_run(
                ctx, candidate_id, FINAL_STAGE, args, result,
                notes=f"exit_code={result.exit_code}",
            )
            ctx.store.set_stage_status(candidate_id, FINAL_STAGE, "failed", note=status)
            results[candidate_id] = status
            continue

        artifact = find_latest_artifact(ctx, since_ts)
        if artifact is None:
            status = "failed: no backtest artifact"
            record_stage_run(ctx, candidate_id, FINAL_STAGE, args, result, notes="no artifact")
            ctx.store.set_stage_status(candidate_id, FINAL_STAGE, "failed", note=status)
            results[candidate_id] = status
            continue

        try:
            stats = _select_stats(parse_backtest_result(artifact), class_name)
            metrics = extract_metrics(stats)
        except (OSError, ValueError, KeyError) as exc:
            status = f"failed: unreadable artifact: {exc}"
            record_stage_run(
                ctx, candidate_id, FINAL_STAGE, args, result,
                artifact_path=str(artifact), notes=status,
            )
            ctx.store.set_stage_status(candidate_id, FINAL_STAGE, "failed", note=status)
            results[candidate_id] = status
            continue

        run_id = record_stage_run(
            ctx, candidate_id, FINAL_STAGE, args, result,
            artifact_path=str(artifact), metrics=asdict(metrics), notes=f"timerange={timerange}",
        )

        if not fitness.passes_hard_filters(metrics, **filters):
            reasons = _filter_reasons(metrics, filters)
            status = "rejected: " + (", ".join(reasons) if reasons else "failed hard filters")
            ctx.store.set_stage_status(candidate_id, FINAL_STAGE, "rejected", note=status)
            results[candidate_id] = status
            continue

        champion_dir = _write_champion(
            ctx, class_name, py_path, params_path, artifact, metrics, timerange, run_id
        )
        status = "passed"
        ctx.store.set_stage_status(
            candidate_id, FINAL_STAGE, "passed", note=f"champion={champion_dir}"
        )
        results[candidate_id] = status
    return results


def _filter_reasons(metrics: fitness.Metrics, filters: dict[str, Any]) -> list[str]:
    """Human-readable reasons for failing the hard filters (empty when passing)."""
    reasons: list[str] = []
    min_trades = filters.get("min_trades")
    if min_trades is not None and metrics.trades < min_trades:
        reasons.append(f"trades {metrics.trades} < {min_trades}")
    max_drawdown_pct = filters.get("max_drawdown_pct")
    if max_drawdown_pct is not None and metrics.max_drawdown_pct > max_drawdown_pct:
        reasons.append(f"max_drawdown_pct {metrics.max_drawdown_pct} > {max_drawdown_pct}")
    min_profit_factor = filters.get("min_profit_factor")
    if min_profit_factor is not None and metrics.profit_factor < min_profit_factor:
        reasons.append(f"profit_factor {metrics.profit_factor} < {min_profit_factor}")
    min_pair_coverage = filters.get("min_pair_coverage")
    if min_pair_coverage is not None and metrics.pair_coverage < min_pair_coverage:
        reasons.append(f"pair_coverage {metrics.pair_coverage} < {min_pair_coverage}")
    if metrics.expectancy <= 0:
        reasons.append("expectancy <= 0")
    return reasons


def _write_champion(
    ctx: PipelineContext,
    class_name: str,
    py_path: Path,
    params_path: Path,
    artifact: Path,
    metrics: fitness.Metrics,
    timerange: str,
    run_id: int,
) -> Path:
    """Copy the champion artifacts into ``output/champions/<Class>/`` + manifest."""
    champion_dir = ctx.root / "output" / "champions" / class_name
    champion_dir.mkdir(parents=True, exist_ok=True)

    py_copy = champion_dir / py_path.name
    params_copy = champion_dir / params_path.name
    artifact_copy = champion_dir / artifact.name
    shutil.copyfile(py_path, py_copy)
    shutil.copyfile(params_path, params_copy)
    shutil.copyfile(artifact, artifact_copy)

    manifest = {
        "class_name": class_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "timerange": timerange,
        "metrics": asdict(metrics),
        "paths": {
            "dir": _rel_to_root(ctx, champion_dir),
            "strategy": _rel_to_root(ctx, py_copy),
            "params": _rel_to_root(ctx, params_copy),
            "artifact": _rel_to_root(ctx, artifact_copy),
        },
        "run_ids": {FINAL_STAGE: run_id},
    }
    (champion_dir / "champion.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return champion_dir


def _rel_to_root(ctx: PipelineContext, path: Path) -> str:
    """Render ``path`` relative to the repo root when possible, else absolute."""
    try:
        return str(Path(path).relative_to(ctx.root))
    except ValueError:
        return str(path)

