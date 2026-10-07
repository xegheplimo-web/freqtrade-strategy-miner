# Quy trình orchestration đầy đủ — freqtrade Strategy Miner

Dự án được xây dựng từ một starter template demo thành **strategy miner hoàn chỉnh** bằng
quy trình orchestration multi-agent có kiểm chứng (2026-10-08):

```
SCAN → PLAN → SPLIT+ROUTE → PARALLEL EXECUTE → VERIFY
  ├ FAIL → FIX → ESCALATE → (INFRA → MAINTENANCE → RESUME)
  └ PASS → INTEGRATE → FULL VERIFY → AUDIT → LEARN
```

**Nguyên tắc cốt lõi: "agent xong ≠ DONE" — chỉ kiểm chứng độc lập của orchestrator mới
đánh dấu DONE.** Agent chỉ ghi file trong worktree riêng; orchestrator (Hermes) sở hữu toàn
bộ thao tác git (commit — merge `--no-ff` — bookkeeping).

## 1. Vai trò & phân công (theo độ khó)

| Vai trò | Agent | Tasks |
|---|---|---|
| Hard — kiến trúc, logic phức tạp | **Devin** | T-202 (hyperopt + validation), T-206 (compiler emit hyperoptable params) |
| Medium — tính năng, module | **Cline** | T-101 (docker runner — nhận lại sau khi Devin stall; orchestrator hoàn thiện 15 dòng cuối), T-201 (pipeline foundation + fast_gate), T-203 (bias checks + walk-forward + final gate) |
| Light — boilerplate, CLI, docs | **OpenCode** | T-204 (robustness stage), T-205 (CLI + entry points + docs) |
| Orchestration · verify · fix · git | **Hermes** | control plane, wave-1 verify, micro-probe live, E2E acceptance, mọi git op |

## 2. Các wave & tiến trình test

| Wave | Tasks | Suite |
|---|---|---|
| 1 | T-101 runner+results · T-102 store+params · T-103 data · T-104 tests+docs | 65 |
| 2a | T-201 pipeline foundation + fast_gate | 121 |
| 2b | T-202 hyperopt+validation · T-203 checks · T-204 robustness | 197 |
| 3 | T-205 CLI · T-206 compiler params | 231 |
| 3b | **T-301 E2E acceptance** (Hermes trực tiếp) | 231 + live E2E |

## 3. Kỷ luật kiểm chứng (verification)

- Không tin self-report / exit code: orchestrator chạy lại suite + ruff + **micro-probe live
  từng stage** trên docker thật trước khi merge.
- **5 defect thật bắt được nhờ micro-probe live** (mocks không bắt được):

| Defect | Phát hiện | Fix |
|---|---|---|
| `KeyError: 'exit_pricing'` — `config.example.json` quá mỏng | probe fast_gate live | mở rộng thành base backtesting hợp lệ (D-011) |
| Sidecar `strategy_name` ≠ tên class variant `_p{k}` → freqtrade raise "Invalid parameter file provided." | probe perturb + đọc source freqtrade | fix `robustness.py` + regression test (D-012) |
| Compiler không emit hyperoptable params → hyperopt chết ("no parameter for this space was found") | probe hyperopt live | T-206: IntParameter buy space + spaces buy/roi/stoploss (D-014) |
| Parser recursive đọc rác rich-table (`'Startup': 100.0`, date row) | replay stdout thật | viết lại parser + 2 test (D-013) |
| CRLF trong stdout phá test T-101 | orchestrator review | `_normalize_newlines` + TestSynthetic (D-010) |

- Chính sách agent stall: không ghi file + CPU idle ≥ 20 phút → kill theo path + reassign
  (D-009); agent chết để lại deliverable gần xong → orchestrator chỉ hoàn thiện gap nhỏ
  (≤ ~15 dòng, không đổi design) và ghi trung thực (D-010).

## 4. E2E acceptance (T-301)

Chạy trên docker `freqtradeorg/freqtrade:stable` (2026.9) + dữ liệu riêng (499.104 nến/pair,
5m, 2022-01→2026-09). Toàn bộ 7 stage chạy live:

- fast-gate 5 backtest → hyperopt **5/5 passed** (66–82s/candidate, sidecar export thật) →
  validation → bias (10 runs: 1 pass, 4 "inconclusive" — lookahead cần ≥10 signals) →
  walk-forward **55 fold runs** (11 folds × 5) → robustness 20 runs (fee + perturb) →
  final_test 5 runs → report/status.
- **Champion path được chứng minh**: forced final dưới smoke config min_trades=1 →
  `output/champions/Miner_000004/` (py + json + zip + champion.json; pf 1.218, expectancy
  +0.0143, 8 trades).
- Kết quả production: cả 5 candidates bị reject đúng bởi các gate (không có champion
  production thật — đó là hành vi đúng với chất lượng genome/hyperopt hiện tại).

**Product findings (chờ quyết định tuning):**
1. hyperopt (40 epochs, SharpeHyperOptLoss) trôi về params cực đoan (vd `buy_rsi_long_max=2`)
   — cần trade-floor trong loss / thu hẹp param ranges / tăng epochs.
2. fast-gate áp production gates (gồm `expectancy>0`) TRƯỚC hyperopt → genome mặc định không
   bao giờ tới bước refine; cần cải thiện genomes hoặc cân chỉnh gate.

## 5. Bản đồ evidence & quyết định

- `.orchestrator/` — INTERFACES.md (hợp đồng interface v1, FROZEN) · TASKS.json ·
  DECISIONS.md (D-001…D-016) · PROJECT_STATE.md · AGENT_RULES.md · VERIFICATION.md.
- `agent_logs/` — card prompt mọi task + result.json + log docker từng stage
  (fast_gate · hyperopt · bias · walk_forward · robustness · final) + log E2E + stall log.
- Git: mỗi task = 1 nhánh `task/T-xxx-*` → merge `--no-ff` vào `main`; control-plane +
  evidence commit riêng, đối chiếu từng bước.

## 6. Tái lập nhanh

```bash
# 1) Môi trường
uv venv .venv --python 3.12
uv pip install --python .venv/Scripts/python.exe pytest ruff pandas pyarrow

# 2) Dữ liệu riêng (không bao giờ trỏ vào hệ live)
docker run --rm -v "$PWD/freqtrade/user_data:/freqtrade/user_data" \
  freqtradeorg/freqtrade:stable download-data \
  --config freqtrade/user_data/config.miner.json \
  --timerange 20220101-20260930 --timeframes 5m

# 3) Gates
.venv/Scripts/python.exe -m pytest -q && .venv/Scripts/ruff.exe check .

# 4) Pipeline (ví dụ từng stage)
PYTHONPATH=src .venv/Scripts/python.exe -m strategy_miner data-audit
PYTHONPATH=src .venv/Scripts/python.exe -m strategy_miner run --count 5 --epochs 40
```

Chi tiết stage/CLI: `docs/pipeline.md`. Quy trình spec đầy đủ: `docs/WORKFLOW.md`.
