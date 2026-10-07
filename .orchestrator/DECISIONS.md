# DECISIONS — freqtrade-strategy-miner

One entry per binding decision. Newest at the bottom. Frozen interfaces/names change only via a new entry.

## D-001 (2026-10-08) — Control plane seeded

- Workspace created via `bootstrap-project.sh`; control plane seeded (state, tasks, rules,
  verification, decisions, interfaces).

## D-002 (2026-10-08) — Program scope (charter)

- Sếp handed over `F:\freqtrade-strategy-miner-template` with the canonical process
  (SCAN→PLAN→SPLIT+ROUTE→…→LEARN). Charter applied: audit whole project, own the roadmap.
- SCAN verdict: template is a demo-only skeleton — WORKFLOW.md stages 0–9 described but code
  exists only for generate/compile/manifest/print-commands. Goal: build the real pipeline as
  working, tested code; freqtrade runs via the local docker image.
- Rounds: M1 foundation (T-101..T-104) -> M2 stages (T-201..T-205) -> E2E acceptance (T-301) ->
  audit + learn. Agents per difficulty matrix; Hermes verifies every artifact.

## D-003 (2026-10-08) — Self-contained runtime (Sếp directive "tải về riêng ra")

- The miner project is fully self-contained: its own `freqtrade/user_data` (data, configs,
  results, hyperopt outputs) and its own downloaded market data. `F:\freqtrade-stable` is
  READ-ONLY reference material (real-ops env, live DB, VPS configs) — never mounted or written.
- Freqtrade runtime = pinned docker image `freqtradeorg/freqtrade:stable` (verified: freqtrade
  2026.9; commands present: backtesting, hyperopt, lookahead-analysis, recursive-analysis,
  download-data). Host shell = bash/MSYS; docker maps `F:/...` paths fine.

## D-004 (2026-10-08) — No editable install (worktree isolation)

- `uv pip install -e .` would bind `strategy_miner` imports to the MAIN checkout and contaminate
  worktree test isolation. Instead: pytest `pythonpath = ["src"]` and CLI via `scripts/miner.py`
  shim + `python -m strategy_miner` with src on sys.path. Dev venv: repo `.venv` (uv, Python
  3.12.14; pytest, ruff, pandas, pyarrow).

## D-005 (2026-10-08) — Hyperopt params via freqtrade native params file

- Verified in freqtrade source (2026.9): end of hyperopt run auto-exports best params to
  `<strategy_file>.json` (`ft_stratparam_v: 1`), and backtesting auto-applies that sidecar when
  present next to the strategy. The miner keeps the sidecar as the single source of truth for
  tuned parameters, stores copies/hashes in the registry; no code re-generation needed.
- `hyperopt-show --best --print-json` gives machine-readable best-epoch params (single JSON line).

## D-006 (2026-10-08) — Registry storage = SQLite `output/miner.db`

- stdlib sqlite3; schema + API frozen in INTERFACES.md §3. Gitignored; champions + manifests stay
  as tracked JSON/py artifacts under `output/`.

## D-007 (2026-10-08) — Repo stays PRIVATE

- `tests/fixtures/backtest-sample.zip` contains real results of a proprietary strategy
  (ZarTest02SL35 lineage). The repo must not be published publicly without redacting fixtures.
- Remote (suggested: private `xegheplimo-web/freqtrade-strategy-miner`) created at first INTEGRATE;
  no push before that. Hermes owns all git ops.

## D-008 (2026-10-08) — Safety rails (from WORKFLOW.md, enforced)

- Pipeline NEVER promotes to live trading; final gate only writes champion artifacts +
  dry-run instructions. Final-test run is one-shot per candidate (recorded, re-run requires
  explicit `--force`). No secrets in repo; empty exchange keys in configs.

## D-009 (2026-10-08) — T-101 reassigned devin -> cline (agent stall policy)

- Devin CLI (print mode, --permission-mode dangerous) produced zero file writes and a frozen
  log for its first ~5.5 min on T-101 (start 02:54:59, killed 03:00:27); CPU near-idle (~2.5s).
  Killed and reassigned to cline. Evidence kept at
  `worktrees/T-101/agent_logs/T-101.devin-stall.log`.
- Record correction: the kill fired at ~5.5 min — earlier than a defensible stall threshold;
  devin print-mode output can buffer, so this was not proof of a hard stall. Reassignment kept
  for velocity (cline delivered T-102/T-103 fast & clean).
- Policy going forward: ground wall-clock with `date` before declaring a stall; a stall is
  no worktree writes AND idle CPU for >= 20 min. Cline is the default fallback for reassignment.

## D-010 (2026-10-08) — T-101 cline timeout; orchestrator completes small gaps

- Cline ran T-101 03:00:42 -> ~03:18 (exit 1, "The operation timed out"; -t 1380s budget).
  It wrote all 4 deliverable files but died mid final-polish: 2 test failures (Windows CRLF
  artifact: captured stdout "hi\r\n" vs asserted "hi\n"), 1 ruff F401, and an unfinished
  `# __PART4__` sentinel in tests/test_results.py (missing TestSynthetic class).
- Orchestrator FIX step (kept within the frozen contract): added `_normalize_newlines`
  (CRLF -> LF) to runner.py and documented it in INTERFACES.md §1; completed TestSynthetic;
  removed the sentinel. Result: 34 tests green + ruff clean in the worktree.
- Policy: when an agent dies leaving a near-complete deliverable, the orchestrator completes
  small gaps (<= ~15 lines, no design changes) and records them honestly; larger gaps are
  re-dispatched to another agent.
- Wave-2 launch mitigation: raise agent timeout budget (`cline -t 2700`) for cards >= 100
  lines and require files-first ordering (already in cards).
