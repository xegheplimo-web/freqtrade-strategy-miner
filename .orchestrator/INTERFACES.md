# INTERFACES — freqtrade-strategy-miner

Contracts between layers. Once a section is marked **FROZEN**, names/signatures change only via a
DECISIONS entry. Agents must not silently change frozen surfaces.

Hard-won facts (verified 2026-10-08 against freqtrade 2026.9 docker image + a real backtest
artifact, see `tests/fixtures/backtest-sample.zip`):

- Backtest result JSON top level: `{"strategy": {<NAME>: <S>, ...}, "strategy_comparison": [...]}`.
- `<S>` carries summary metrics AND `trades` (list of trade dicts), `results_per_pair`
  (includes a `TOTAL` row that must be excluded), `pairlist` (list of pair strings),
  `periodic_breakdown` (`{day,week,month,year,weekday}` -> lists of
  `{date,date_ts,profit_abs,wins,draws,losses,trades,profit_factor}`), `daily_profit`
  (`[[date, profit_abs], ...]`), `exit_reason_summary`, `left_open_trades`.
- Hyperopt: freqtrade **auto-exports** best params at the end of a hyperopt run to
  `<strategy_file>.json` (same dir as the strategy `.py`), format
  `{"strategy_name": ..., "params": {...}, "ft_stratparam_v": 1, "export_time": ...}`.
  That file is **auto-applied** by backtesting/live when present next to the strategy.
  `freqtrade hyperopt-show --best --print-json` prints a one-line JSON of the best epoch
  (keys: `params`, `minimal_roi`, `stoploss`, optional `trailing_*`, `max_open_trades`).

## 1. runner.py — docker execution surface (FROZEN 2026-10-08)

```python
DEFAULT_IMAGE = "freqtradeorg/freqtrade:stable"
CONTAINER_USERDATA = "/freqtrade/user_data"

class RunnerError(RuntimeError): ...

@dataclass(frozen=True)
class RunResult:
    argv: tuple[str, ...]      # full host-level command actually executed
    exit_code: int             # 124 on timeout; real exit code otherwise
    stdout: str                # combined stdout+stderr, decoded utf-8 errors="replace",
                               # ANSI escape sequences stripped
    duration_s: float
    log_path: Path | None = None   # set when log_dir/log_name given

class Runner(Protocol):            # any object with these two methods is accepted
    def run(self, args: Sequence[str], *, timeout_s: int = 3600,
            log_dir: Path | None = None, log_name: str | None = None) -> RunResult: ...
    def map_path(self, host_path: Path) -> str: ...

class DockerRunner:
    def __init__(self, user_data_dir: Path, *, image: str = DEFAULT_IMAGE,
                 docker_bin: str = "docker") -> None: ...
    def map_path(self, host_path: Path) -> str:
        # host path under user_data_dir -> "/freqtrade/user_data/<rel>" (posix, forward slashes)
        # ValueError if outside user_data_dir
    def build_command(self, args: Sequence[str]) -> list[str]:
        # exactly: [docker_bin, "run", "--rm", "-v",
        #           f"{posix(user_data_dir)}:{CONTAINER_USERDATA}", image, *args]
        # posix(p) = str(p).replace("\\", "/")
    def run(self, args, *, timeout_s=3600, log_dir=None, log_name=None) -> RunResult:
        # subprocess.run(cmd, capture_output=True, timeout=timeout_s)
        # timeout -> exit_code 124 + stdout "TIMEOUT after <n>s\n" + partial output
        # docker binary missing -> RunnerError
        # log_dir/log_name given -> also write full combined output to log_dir/log_name
```

`args` are the freqtrade CLI args in CONTAINER paths (e.g. `["backtesting", "--config",
"/freqtrade/user_data/config.miner.json", ...]`). Callers map host paths with `map_path()`.

## 2. results.py — backtest artifact parsing (FROZEN 2026-10-08)

```python
@dataclass(frozen=True)
class StrategyStats:
    strategy_name: str
    summary: dict[str, Any]          # full strategy summary dict (raw, unchanged)
    trades: tuple[dict[str, Any], ...]
    per_pair: tuple[dict[str, Any], ...]   # results_per_pair WITHOUT the TOTAL row
    pairlist: tuple[str, ...]

def load_backtest_json(path: Path) -> dict:
    # accepts a .zip (reads the single non-config *.json entry; prefers
    # "backtest-result-*.json") or a plain .json file. Raises ValueError on
    # unreadable/malformed content.

def parse_backtest_result(path: Path) -> dict[str, StrategyStats]:
    # {strategy_name: StrategyStats}; ValueError if no strategies.

def extract_metrics(stats: StrategyStats) -> fitness.Metrics:
    # total_return_pct    = summary["profit_total"] * 100
    # max_drawdown_pct    = summary["max_drawdown_account"] * 100
    # sharpe, sortino, profit_factor, expectancy = summary[...] as-is
    # trades              = summary["total_trades"]
    # pair_coverage       = (# per_pair rows with trades > 0) / len(pairlist)   (0.0 if empty)
    # stability           = positive months / total months from
    #                       summary["periodic_breakdown"]["month"] (profit_abs > 0);
    #                       missing/empty -> 0.0
    # complexity          = 1.0 (pipeline may override)

def positive_months_fraction(summary: dict) -> float:
    # independent helper used by extract_metrics; same rule.

def drop_top_n_trades(trades: Sequence[dict], n: int) -> dict:
    # sorts by profit_abs desc, drops first n;
    # returns {"trades_after": int, "profit_abs_after": float, "profit_factor_after": float | None}
    # pf = gross_profit / abs(gross_loss); None when gross_loss == 0
```

## 3. store.py — registry (FROZEN 2026-10-08)

SQLite via stdlib `sqlite3`, `PRAGMA foreign_keys=ON`. DB path: `<root>/output/miner.db`
(gitignored). All timestamps UTC ISO-8601 strings. JSON dumps `ensure_ascii=False`, sorted keys.

```python
class Store:
    def __init__(self, db_path: Path) -> None: ...            # creates schema if absent
    def upsert_candidate(self, genome, py_path: str, params_path: str | None = None) -> int
        # keyed by genome.class_name; returns candidate id; updates paths on repeat
    def get_candidate(self, candidate_id: int) -> dict        # KeyError if missing
    def list_candidates(self) -> list[dict]                   # ordered by strategy_id
    def candidate_by_class(self, class_name: str) -> dict | None
    def record_run(self, candidate_id: int, stage: str, argv: Sequence[str], exit_code: int,
                   duration_s: float, artifact_path: str | None = None,
                   metrics: dict | None = None, notes: str | None = None) -> int
        # ValueError if candidate_id unknown; returns run id
    def runs_for(self, candidate_id: int, stage: str | None = None) -> list[dict]
        # ordered by id
    def set_stage_status(self, candidate_id: int, stage: str, status: str,
                         note: str | None = None) -> None       # upsert
    def stage_status(self, candidate_id: int, stage: str) -> str | None
    def shortlist(self) -> list[int]     # ids with stage "fast_gate" status "shortlisted"
```

Tables (implementer may add columns/tables additively; these must exist):

- `candidates(id INTEGER PRIMARY KEY, strategy_id INT, class_name TEXT UNIQUE, genome_json TEXT,
  py_path TEXT, params_path TEXT, created_at TEXT)`
- `runs(id INTEGER PRIMARY KEY, candidate_id INT REFERENCES candidates(id), stage TEXT,
  argv_json TEXT, exit_code INT, duration_s REAL, artifact_path TEXT, metrics_json TEXT,
  notes TEXT, created_at TEXT)`
- `stages(candidate_id INT REFERENCES candidates(id), stage TEXT, status TEXT, note TEXT,
  updated_at TEXT, PRIMARY KEY(candidate_id, stage))`

Stage names (frozen vocabulary): `fast_gate`, `hyperopt`, `validation`, `bias`, `walk_forward`,
`robustness`, `final_test`. Statuses: `pending`, `running`, `passed`, `failed`, `shortlisted`,
`rejected`, `skipped`.

## 4. params.py — hyperopt params flow (FROZEN 2026-10-08)

```python
def write_params_file(strategy_py: Path, params: dict, *, strategy_name: str) -> Path
    # writes <strategy_py>.json (with_suffix(".json")) in freqtrade native format:
    # {"strategy_name": ..., "params": params, "ft_stratparam_v": 1, "export_time": <iso>}
    # returns the path. Values must be JSON-native (json.dumps default=float).

def read_params_file(path: Path) -> dict          # returns the inner "params" dict
def perturb_params(params: dict, pct: float, *, rng: random.Random) -> dict
    # deep-copy walk; bool unchanged; None unchanged; str unchanged;
    # int v > 0 -> max(1, int(round(v * (1 + delta)))); other ints unchanged;
    # float -> round(v * (1 + delta), 4); dict/list recurse; delta = rng.uniform(-pct, +pct)
def load_hyperopt_show_json(text: str) -> dict
    # parse the LAST line of `hyperopt-show --best --print-json` stdout that json.loads()
    # into a dict; ValueError if none found
```

## 5. data.py — data stage (FROZEN 2026-10-08)

```python
@dataclass(frozen=True)
class PairAudit:
    pair: str; timeframe: str; rows: int; first_ts: str; last_ts: str
    duplicates: int; gaps: int; max_gap_minutes: float
    ohlc_violations: int; zero_volume_rows: int; path: str  # "" if missing
    missing: bool

@dataclass(frozen=True)
class DataAudit:
    pairs: tuple[PairAudit, ...]; timeframes: tuple[str, ...]
    timerange: str | None; ok: bool; issues: tuple[str, ...]

def feather_filename(pair: str, timeframe: str, *, trading_mode: str = "futures") -> str
    # "BTC/USDT:USDT" -> "BTC_USDT_USDT-5m-futures.feather" (futures)
    # spot -> "BTC_USDT_USDT-5m.feather"
def pair_data_path(data_dir: Path, pair: str, timeframe: str, *, exchange: str = "binance",
                   trading_mode: str = "futures") -> Path
    # data_dir/<exchange>/futures/<file> for futures; data_dir/<exchange>/<file> for spot

def audit_data(data_dir: Path, pairs: Sequence[str], timeframes: Sequence[str], *,
               timerange: str | None = None, exchange: str = "binance",
               trading_mode: str = "futures") -> DataAudit
    # feather read via pyarrow; gap = consecutive ts diff > 1.5 * timeframe interval;
    # duplicates = repeated timestamps; ohlc_violations = high < max(open, close) or
    # low > min(open, close) or high < low; ok=False if any missing file,
    # ohlc_violations > 0, duplicates > 0, or (timerange given and
    # missing_frac > 0.05 of expected rows).
def download_args(config_container_path: str, pairs: Sequence[str], timeframes: Sequence[str],
                  timerange: str, *, exchange: str = "binance") -> list[str]
    # exactly: ["download-data", "--config", cfg, "--exchange", exchange,
    #           "--pairs", *pairs, "--timeframes", *timeframes, "--timerange", timerange]
    # trading mode (futures) comes from the freqtrade config file.
def data_checksums(data_dir: Path, pairs, timeframes, *, exchange="binance",
                   trading_mode="futures") -> dict[str, str]
    # {relative posix filename: sha256 hex} for files that exist
```

Data dirs: host `<root>/freqtrade/user_data/data` (gitignored, downloaded fresh — never reuse
other project's data).

## 6. CLI & entry conventions (FROZEN 2026-10-08)

- Entry: `scripts/miner.py` shim (inserts `src/` into sys.path, calls `strategy_miner.cli.main()`),
  plus `python -m strategy_miner` (`__main__.py`). NO editable install (isolation rule D-004).
- Commands (names frozen): `generate`, `data-download`, `data-audit`, `fast-gate`, `hyperopt`,
  `validate`, `bias`, `walk-forward`, `robust`, `final`, `report`, `status`, `run`.
- Common args: `--root PATH` (default: repo root of the shim), `--config PATH` (default
  `config/miner.json`).
- Runtime freqtrade config: `<root>/freqtrade/user_data/config.miner.json` (gitignored, generated
  from `config/miner.json` + `freqtrade/config.example.json` base: pairs, exchange, timeframe,
  trading_mode). Container path: `/freqtrade/user_data/config.miner.json`.
- Generated strategies: `<root>/freqtrade/user_data/strategies/generated/<Class>.py` (+ `.json`
  params sidecar when hyperopted). Container: `/freqtrade/user_data/strategies/generated`.

## 7. pipeline package layout (FROZEN layout, wave 2)

```
src/strategy_miner/pipeline/__init__.py    # PipelineContext + record helpers  [T-201]
src/strategy_miner/pipeline/fast_gate.py   # [T-201]
src/strategy_miner/pipeline/refine.py      # hyperopt + validation            [T-202]
src/strategy_miner/pipeline/checks.py      # bias + walk-forward + final      [T-203]
src/strategy_miner/pipeline/robustness.py  # fee/perturb/drop-top             [T-204]
```

```python
@dataclass(frozen=True)
class PipelineContext:
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
```

## Freeze record

- 2026-10-08 — v1 frozen by Hermes round-0 (before T-101..T-104 dispatch); facts above verified
  against freqtrade 2026.9 image + live artifact fixture + freqtrade source
  (`freqtrade/strategy/hyper.py`, `optimize/hyperopt_tools.py`).
