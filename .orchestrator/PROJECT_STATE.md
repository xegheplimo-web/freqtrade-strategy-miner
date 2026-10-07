# PROJECT STATE — freqtrade-strategy-miner

**Project**: freqtrade-strategy-miner — turn the demo starter template into a working, verified,
self-contained Freqtrade strategy-mining pipeline (genome -> compile -> backtest -> score ->
shortlist -> hyperopt -> validation -> bias checks -> walk-forward -> robustness -> final test ->
champion), driven by config + registry, orchestrated around the freqtrade docker image.
**Repo**: `F:/freqtrade-strategy-miner-template` · **Worktrees**: `F:/freqtrade-strategy-miner-worktrees/<TASK-ID>`

## Current milestone
**M3 — CLI + E2E acceptance** (2026-10-08): all pipeline stages merged (suite 197); fast_gate
live-verified (real docker backtest + artifact + store); perturb sidecar fix live-verified
(freqtrade loads variant params). Next: T-205 CLI (opencode) -> T-301 E2E acceptance.

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
- 2026-10-08 — wave 2b DONE + merged: T-202 hyperopt+validation (devin), T-203 bias+WF+final
  (cline), T-204 robustness (opencode; orchestrator fixed perturb sidecar strategy_name ->
  variant, live-verified). Suite 197. fast_gate live smoke green (315K-candle backtest,
  exit 0, artifact + store records ok). config.example.json expanded to a valid backtesting
  base (pricing/timeout/order keys — smoke caught the KeyError: exit_pricing gap).

## In progress
- T-205 CLI + entry points + docs (opencode, worktree T-205, base 4029dab).

## Next up
- T-301 E2E acceptance (live docker run: data-audit -> fast-gate -> hyperopt -> validate ->
  bias -> walk-forward -> robust -> final -> report).

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
