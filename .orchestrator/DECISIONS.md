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

## D-011 — config.example.json expanded to a valid backtesting base
- fast_gate live smoke failed (`KeyError: 'exit_pricing'`, exit 1, no artifact). Root cause:
  `sync_ft_config` regenerates `config.miner.json` from `freqtrade/config.example.json` on
  every run — the demo example was not a valid freqtrade base (missing pricing/timeout keys).
- Fix: expanded the example with dry_run_wallet, liquidation_buffer, unfilledtimeout,
  entry_pricing, exit_pricing, ccxt_config/ccxt_async_config, pair_blacklist. Lesson: the
  committed EXAMPLE must be a fully valid freqtrade config; runtime configs cannot carry
  manual fixes. Smoke re-run: exit 0, 315K-candle backtest, artifact + store records OK.

## D-012 — perturb sidecar: strategy_name must equal the variant class name
- freqtrade `strategy/hyper.py` loads `<strategy_file>.json` and raises
  "Invalid parameter file provided." when its `strategy_name` != the loaded class name.
- T-204 originally wrote the ORIGINAL class name into `<Class>_p{k}.json` while renaming
  the class to `<Class>_p{k}` → every perturb run would fail live (mocks missed it).
- Fix: write `strategy_name=variant`; regression asserts added (class renamed in .py AND
  raw sidecar name). LIVE-verified: variant backtest exit 0, "Loading parameters from file"
  in stdout, no invalid-param error.

## D-013 — recursive-analysis parser fixed against real output
- Live run showed the real stdout is a rich box table (│ data rows, ┃ header, a
  "Startup candle ... 100%" progress bar, log lines containing "%"). The fixture-based
  parser captured garbage ('Startup': 100.0, date rows, '│') → would reject every
  candidate (worst_pct 100 >= threshold).
- Fix: accept only data rows (identifier first cell; remaining cells "%" or "-"); record
  max ABSOLUTE pct per row. Noise + real-format + negative tests added. Live replay on
  the real stdout: {ema_fast: 0.0, ema_slow: 0.001, rsi: 0.001}.
- lookahead-analysis live-verified too: CSV export + parse OK (has_bias=False, 20 signals).
- Harness note: micro-tests must mirror the stage's own setup (run_bias_checks mkdirs the
  analysis dir; a bare argv runner must do the same).

## D-014 — hyperopt spaces: emit buy params; drop the unused 'sell' space (T-206)
- Live hyperopt failed: "The 'buy' space is included into the hyperoptimization but no
  parameter for this space was found in your Strategy." — compiled strategies hardcoded
  every value; no hyperoptable parameters existed.
- Design: T-206 (devin) makes the compiler emit buy-space IntParameters for the entry RSI
  thresholds (defaults = genome values → behavior unchanged without a sidecar) and reduces
  `--spaces` to buy/roi/stoploss (exits are signal flips → no sell params; freqtrade
  rejects empty spaces). ROI/stoploss need no strategy params (freqtrade defaults).
- Status: T-206 merged (devin; 198 tests, ruff clean). Live re-verification (hyperopt exit 0
  + sidecar with buy params) runs before E2E; compiler change is orchestrator-designed and
  implemented exactly per card.

## D-015 — E2E acceptance harness: relaxed-gate config for T-301
- E2E run #1 (production `config/miner.json`, count=3): data-audit ok (499,104 rows/pair,
  36 zero-volume rows noted); fast-gate generated+backtested 3/3, **rejected 3/3** — legit:
  PF 0.66–0.86, expectancy negative, dd up to 43.8% (filters: pf >= 1.15, dd <= 25). The
  pipeline correctly stopped at the empty shortlist ("hyperopt: stopped: empty id list").
- Finding (product-tuning, deferred): fast_gate applies production gates BEFORE hyperopt, so
  default-parameter genomes that lose money never reach parameter refinement. Gate
  calibration vs genome quality is a tuning decision for Sếp — not changed here.
- Acceptance harness: `config/miner.e2e-acceptance.json` (same data/timeranges; relaxed
  filters dd <= 60, pf >= 0.5, coverage >= 0.5, min_trades unchanged) — purpose: exercise
  EVERY stage mechanically end-to-end on live docker. Production config untouched.
## D-016 — T-301 E2E acceptance: results + product findings
- All stages live-exercised (docker freqtrade 2026.9 + own data): data-audit (499,104 rows/pair)
  -> fast-gate (5 backtests) -> hyperopt (5/5 passed, 66-82s each, real exports) -> validation
  -> bias (10 runs; 1 passed; 4 "inconclusive: too few trades caught (0/10)" — lookahead needs
  >=10 signals) -> walk-forward (55 fold runs = 11 folds x 5) -> robustness (20 runs: fee +
  perturb x 5; drop-top pure-compute) -> final_test (5 runs) -> report + status.
- Champion write path proven live via forced final on Miner_000004 under a min_trades=1 smoke
  config: output/champions/Miner_000004/{py,json,zip,champion.json} (pf 1.218, exp +0.014,
  8 trades). Production acceptance outcome stands: 5/5 rejected (correct gates).
- PRODUCT FINDINGS (tuning decisions deferred to Sếp):
  1. hyperopt (40 epochs, SharpeHyperOptLoss) drifts to degenerate sparse-entry params
     (e.g. buy_rsi_long_max=2) — needs loss trade-floor / tighter param ranges / more epochs.
  2. fast_gate (production gates incl. hardcoded expectancy>0) rejects default genomes BEFORE
     hyperopt can refine them; pipeline effectively needs either better genomes or gate
     calibration (e.g. drop expectancy from pre-hyperopt gate).
- CLI note: --config/--root global; --ids/--epochs/--force per-subcommand; report/status take
  no --ids.
- freqtrade-stable audit: 1 regenerated .pyc only; no data/config/strategy writes.

## D-017 — fast-gate screen split: `fast_gate_filters` (pre-hyperopt) vs `hard_filters` (production)
- Resolves D-015/D-016 finding #2: fast_gate applied the production gates (pf >= 1.15, dd <= 25,
  hardcoded expectancy > 0) BEFORE hyperopt, so raw default genomes never reached refinement
  (E2E: 3/3 rejected at stage 3).
- Fix: stage 3 screens with `fast_gate_filters` when configured — loose screen floor
  (min_trades 300, dd 80, pf 0.0, coverage 0.6, `min_expectancy: null` = skip; shortlist
  ranked by fitness) — and falls back to `hard_filters` when the key is absent (acceptance
  configs unchanged). `fitness.passes_hard_filters` gained `min_expectancy` (default 0.0,
  None skips the check). Production `hard_filters` still applies at validation + final, and is
  now meaningful because params get refined in between. Stage notes renamed to
  "failed/passed fast-gate filters".

## D-018 — hyperopt hardening: tight ranges + min-trades floor + fixed random-state
- Resolves D-016 finding #1 (degenerate drift: `buy_rsi_long_max=2`, 39 trades / 3y).
  Root cause: wide search spaces (1..49 / 51..99) + no trade floor.
- Fix: compiler template ranges narrowed to the genome neighbourhood
  (buy_rsi_long_max 20..45, buy_rsi_short_min 55..80; defaults = genome values);
  `hyperopt.min_trades` (900, config) -> `--min-trades` (epochs below the floor are
  ineligible); `hyperopt.random_state` (42) -> `--random-state` (reproducible runs). Both
  flags optional — argv stays byte-identical when unset (tests assert both modes).
- M4 campaign config: candidate_count 240, epochs 100, selection min 12. Also fixed the
  commands.py example argv (`--spaces buy sell roi stoploss` -> `buy roi stoploss`; the sell
  space was rejected live in D-014).
