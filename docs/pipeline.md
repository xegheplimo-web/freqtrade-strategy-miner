# Pipeline Strategy Miner

Tài liệu nối CLI (`python -m strategy_miner`, `scripts/miner.py`) với các stage
trong code. Mọi tên lệnh và stage tuân theo `.orchestrator/INTERFACES.md` §6–§7.

## 1. Kiến trúc tổng quan

```
Stage 0  Data         ── src/strategy_miner/data.py
Stage 1  Generate     ── generator.py + orchestrator.generate_batch
Stage 2  Compile      ── compiler.write_strategy (gọi trong Stage 1 và 3)
Stage 3  Fast gate    ── pipeline/fast_gate.py :: run_fast_gate
Stage 4  Hyperopt     ── pipeline/refine.py   :: run_hyperopt
Stage 5  Validation   ── pipeline/refine.py   :: run_validation
Stage 6  Bias checks  ── pipeline/checks.py   :: run_bias_checks
Stage 7  Walk-forward ── pipeline/checks.py   :: run_walk_forward
Stage 8  Robustness   ── pipeline/robustness.py :: run_robustness
Stage 9  Final test   ── pipeline/checks.py   :: run_final
```

Nền tảng chung: `pipeline/__init__.py` (`PipelineContext`, `make_context`,
`sync_ft_config`, `backtest_args`, `find_latest_artifact`, `record_stage_run`)
trên các module gốc `runner.py` (chạy freqtrade qua docker), `results.py` (đọc
artifact backtest), `store.py` (SQLite `<root>/output/miner.db`), `params.py`
(params sidecar hyperopt), `fitness.py` (score + hard filters).

Chuỗi lọc: fast-gate (backtest TRAIN + hard filters + shortlist top) →
hyperopt (TRAIN, sinh `<Class>.json`) → validation (VALIDATION + chống
drawdown bùng nổ / profit-factor sụp) → bias (lookahead + recursive, bias là
hard reject) → walk-forward (median profit_factor ≥ 1.0, expectancy dương) →
robustness (fee stress, perturb params, drop-top-trades) → final (FINAL_TEST
một lần, copy champion).

## 2. Lệnh CLI

Mọi lệnh đều có `--root PATH` (mặc định: repo root) và `--config PATH` (mặc
định: `<root>/config/miner.json`). Mỗi lệnh in một dòng JSON tóm tắt
(`json.dumps(..., default=str, ensure_ascii=False)`); exit code `0` khi thành
công, `1` khi có trạng thái `failed...` (`rejected`/`skipped` là kết quả lọc
hợp lệ, vẫn exit `0`), `2` khi lỗi usage/config.

| Lệnh | Việc làm | Ví dụ |
|---|---|---|
| `generate` | sinh strategy + `output/manifests/latest_generation.json` | `python -m strategy_miner generate --count 50` |
| `data-download` | chạy `download-data` cho `train_start-final_end` (hoặc `--timerange`) | `python -m strategy_miner data-download` |
| `data-audit` | kiểm tra feather data (`ok` + `issues`) | `python -m strategy_miner data-audit` |
| `fast-gate` | backtest TRAIN, shortlist top | `python -m strategy_miner fast-gate --count 50` |
| `hyperopt` | hyperopt TRAIN (`--ids` hoặc shortlist; `--epochs`) | `python -m strategy_miner hyperopt --epochs 300` |
| `validate` | backtest VALIDATION (`--ids` hoặc hyperopt `passed`) | `python -m strategy_miner validate --ids 1 2` |
| `bias` | lookahead + recursive (`--ids` hoặc validation `passed`) | `python -m strategy_miner bias` |
| `walk-forward` | các fold out-of-sample (`--max-folds`) | `python -m strategy_miner walk-forward --max-folds 4` |
| `robust` | fee/perturb/drop-top (`--ids` hoặc walk_forward `passed`) | `python -m strategy_miner robust` |
| `final` | FINAL_TEST một lần + champions (`--force` để test lại) | `python -m strategy_miner final` |
| `report` | ghi `output/report.md` (bảng candidate × stage + metrics TRAIN) | `python -m strategy_miner report` |
| `status` | đếm candidate theo stage/status từ store | `python -m strategy_miner status` |
| `run` | E2E: audit (warn-only) → fast-gate → hyperopt → validate → bias → walk-forward → robust → final; dừng khi danh sách id rỗng | `python -m strategy_miner run --count 50` |

`scripts/miner.py` là shim (chèn `<repo>/src` vào `sys.path`, gọi
`strategy_miner.cli.main()`), dùng được từ mọi cwd:

```bash
python scripts/miner.py --help
python scripts/miner.py status --root F:/path/to/worktree
```

## 3. File/artifacts sinh ra

- `freqtrade/user_data/config.miner.json` — config runtime (sinh từ
  `freqtrade/config.example.json` + `config/miner.json`, gitignored).
- `freqtrade/user_data/data/<exchange>/futures/*.feather` — nến OHLCV tải về
  (gitignored, luôn tải mới).
- `freqtrade/user_data/strategies/generated/<Class>.py` — strategy sinh ra;
  `<Class>.json` kề bên là params sidecar do hyperopt xuất tự động.
- `freqtrade/user_data/strategies/robustness/` — bản sao đổi tên khi perturb.
- `freqtrade/user_data/backtest_results/backtest-result-*.zip` — artifact
  backtest (+ `.last_result.json` trỏ bản mới nhất).
- `freqtrade/user_data/analysis/lookahead_<Class>.csv` — xuất lookahead-analysis.
- `output/miner.db` — SQLite registry (candidates, runs, stages; gitignored).
- `output/manifests/latest_generation.json` — manifest của `generate`.
- `output/report.md` — bảng candidate × trạng thái từng stage + metrics TRAIN.
- `output/champions/<Class>/` — strategy `.py` + params `.json` + artifact zip
  + `champion.json` của candidate qua final.
- `output/robustness/<Class>.json` — chi tiết 3 check robustness.
- `agent_logs/*.log` — log từng lần chạy freqtrade.

## 4. `config/miner.json` — các key chính

`seed`, `candidate_count`, `timeframe`, `can_short`, `train_timerange`,
`validation_timerange`, `final_test_timerange`, `exchange`, `trading_mode`,
`pairs`, `hard_filters` (`min_trades`, `max_drawdown_pct`,
`min_profit_factor`, `min_pair_coverage`), `selection` (`top_fraction`,
`min_candidates`). Key tùy chọn (có default trong code): `hyperopt.epochs`
(mặc định 300), `bias.timerange` (mặc định validation) và
`bias.recursive_max_pct` (mặc định 5.0), `robustness` (`base_fee` 0.0005,
`fee_multiplier` 1.5, `perturb_pct` 0.07, `perturb_runs` 3, `drop_top_frac` 0.05).

## 5. Nguyên tắc an toàn

- Pipeline chỉ gọi backtest/hyperopt/analysis của freqtrade — không có lệnh
  live-trading hay auto-deploy trong code.
- Final test là one-shot: candidate đã có run `final_test` sẽ `skipped`, chỉ
  test lại khi truyền `--force`.
- Thư mục data (`freqtrade/user_data/data`), DB và config runtime đều
  gitignored; không commit dữ liệu tải về hay artifact sinh ra.
