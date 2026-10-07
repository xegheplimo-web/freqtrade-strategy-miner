# PROJECT STATE — freqtrade-strategy-miner

**Project**: freqtrade-strategy-miner — turn the demo starter template into a working, verified,
self-contained Freqtrade strategy-mining pipeline (genome -> compile -> backtest -> score ->
shortlist -> hyperopt -> validation -> bias checks -> walk-forward -> robustness -> final test ->
champion), driven by config + registry, orchestrated around the freqtrade docker image.
**Repo**: `F:/freqtrade-strategy-miner-template` · **Worktrees**: `F:/freqtrade-strategy-miner-worktrees/<TASK-ID>`

## Current milestone
**M2 — pipeline stages** (2026-10-08): wave-1 foundation merged (suite 121); fast_gate stage
merged; hyperopt/validation (T-202), bias/walk-forward/final (T-203), robustness (T-204) in
flight in parallel worktrees. Next: CLI (T-205) -> E2E acceptance (T-301) on real docker.

## Completed
- 2026-10-08 — SCAN: template audited (demo-only; WORKFLOW.md stages 3–10 unimplemented);
  freqtrade-stable mapped (live ops env — READ-ONLY reference); docker image freqtrade 2026.9
  verified (backtesting/hyperopt/lookahead/recursive/download-data present); agent CLIs + guard
  hook + kit preflight green (11/11).
- 2026-10-08 — workspace bootstrapped (control plane seeded); venv `.venv` (uv, Python 3.12.14:
  pytest+ruff+pandas+pyarrow); real backtest fixture -> `tests/fixtures/backtest-sample.zip`;
  runtime freqtrade config `freqtrade/user_data/config.miner.json`; own data download (5m,
  BTC/ETH/SOL, 2022-2026) requested into own user_data (self-contained; live env untouched).
- 2026-10-08 — interfaces v1 FROZEN (.orchestrator/INTERFACES.md).
- 2026-10-08 — wave 1 DONE + merged: T-101 runner+results (orchestrator CRLF fix, D-010),
  T-102 store+params, T-103 data (real-data audit ok: 499K candles/pair, 0 dup/gap/ohlc-bad),
  T-104 tests+docs. Full suite 99 -> 121 after T-201.
- 2026-10-08 — T-201 pipeline foundation + fast_gate merged (make_context, sync_ft_config,
  backtest_args, find_latest_artifact, record_stage_run; 22 new tests; ruff clean).

## In progress
- Wave 2b (parallel, base 708109f): T-202 refine (devin) · T-203 checks (cline) ·
  T-204 robustness (opencode).

## Next up
- Wave 2b verify + merge (Hermes) -> T-205 CLI + docs -> T-301 E2E acceptance (live docker run:
  generate -> backtest -> hyperopt -> validate -> bias -> walk-forward -> robust -> final).

## Blocked
- none

## Key decisions (see DECISIONS.md)
- D-003 self-contained runtime (own user_data + own data; freqtrade-stable read-only)
- D-004 no editable install (worktree isolation)
- D-005 hyperopt params via freqtrade native params-file mechanism
- D-006 registry = SQLite `output/miner.db`
- D-007 repo stays PRIVATE (fixture contains proprietary strategy results)
- D-009 agent stall policy (wall-clock grounded; cline default fallback)
- D-010 agent timeout -> orchestrator completes small gaps (<= ~15 lines) and records honestly
