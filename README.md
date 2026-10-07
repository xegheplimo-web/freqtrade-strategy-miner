# Freqtrade Strategy Miner — Template

Mẫu kiến trúc để tự động hóa pipeline:

`Generate → Freqtrade Backtest → Score → Shortlist → Hyperopt → Validation → Bias Checks → Walk-forward → Champion`

> Đây là **starter template**, không phải bot live-ready. Mặc định không chứa API key và không tự gửi lệnh giao dịch.

## 1. Mục tiêu

- Sinh strategy Python đúng dạng `IStrategy` của Freqtrade.
- Mỗi strategy được sinh từ một `Genome` có cấu trúc, thay vì để LLM viết Python tùy ý.
- Giữ Train / Validation / Final Test tách biệt.
- Backtest hàng loạt trước, chỉ Hyperopt top candidate.
- Chặn lookahead bias / recursive indicator issues trước khi chọn champion.
- Ghi lại genome, seed và manifest để tái lập kết quả.

## 2. Cấu trúc

```text
freqtrade-strategy-miner-template/
├── config/miner.json
├── docs/WORKFLOW.md
├── freqtrade/
│   ├── config.example.json
│   └── user_data/
│       ├── strategies/generated/
│       ├── backtest_results/
│       └── hyperopt_results/
├── output/
│   ├── champions/
│   └── manifests/
├── scripts/
│   ├── run_demo.py
│   ├── run_demo.ps1
│   └── run_demo.sh
├── src/strategy_miner/
│   ├── genome.py
│   ├── generator.py
│   ├── compiler.py
│   ├── commands.py
│   ├── fitness.py
│   └── orchestrator.py
└── tests/
    └── test_generation.py
```

## 3. Chạy demo không cần Freqtrade

Windows PowerShell:

```powershell
python .\scripts\run_demo.py --count 10
```

Linux/macOS:

```bash
python3 ./scripts/run_demo.py --count 10
```

Demo sẽ:

1. Sinh genome ngẫu nhiên có seed.
2. Compile thành `IStrategy`.
3. Kiểm tra syntax Python.
4. Ghi manifest JSON.
5. In các lệnh Freqtrade cần chạy ở bước tiếp theo.

## 4. Khi đã có Freqtrade

Tạo `user_data` của Freqtrade hoặc trỏ đường dẫn cấu hình đến thư mục trong template.

Ví dụ backtest một candidate:

```bash
freqtrade backtesting \
  --config ./freqtrade/config.json \
  --strategy Miner_000001 \
  --strategy-path ./freqtrade/user_data/strategies/generated \
  --timerange 20220101-20241231 \
  --export trades
```

Hyperopt chỉ candidate đã qua vòng backtest/validation sơ bộ:

```bash
freqtrade hyperopt \
  --config ./freqtrade/config.json \
  --strategy Miner_000001 \
  --strategy-path ./freqtrade/user_data/strategies/generated \
  --timerange 20220101-20241231 \
  --spaces buy sell roi stoploss \
  --hyperopt-loss MultiMetricHyperOptLoss \
  -e 300
```

Không nên đưa `trailing` vào search space đầu tiên. Tối ưu trailing riêng sau khi cấu trúc và các parameter khác đã ổn định.

## 5. Nguyên tắc dữ liệu

Ví dụ:

```text
TRAIN       2022-01-01 → 2024-12-31
VALIDATION  2025-01-01 → 2025-12-31
FINAL TEST  2026-01-01 → 2026-09-30
```

`FINAL TEST` chỉ mở một lần ở cuối pipeline. Không được sửa strategy dựa trên kết quả Final Test rồi gọi đó là OOS nữa.

## 6. Pipeline production khuyến nghị

```text
Historical Futures Data
        ↓
Data quality checks
        ↓
Genome Generator
        ↓
Compile IStrategy
        ↓
Syntax / static checks
        ↓
Fast TRAIN backtest
        ↓
Hard filters
        ↓
Top 1–5%
        ↓
Hyperopt on TRAIN
        ↓
VALIDATION
        ↓
Lookahead analysis
        ↓
Recursive analysis
        ↓
Walk-forward
        ↓
Multi-pair robustness
        ↓
Fee / slippage / parameter perturbation
        ↓
FINAL TEST
        ↓
Champion
        ↓
Dry-run
        ↓
Small-size live
```

## 7. Fitness

Không xếp hạng bằng profit đơn thuần. Template có hàm score minh họa dựa trên:

- return
- Sharpe
- Sortino
- profit factor
- expectancy
- max drawdown
- số trade
- pair coverage
- stability
- complexity penalty

Thay trọng số bằng objective phù hợp với bot thật trước khi production.

## 8. An toàn

- Không commit API key.
- Dùng config secrets riêng khi live.
- Không tự động promote strategy sang live chỉ vì đứng #1 backtest.
- Mọi champion phải qua dry-run.
- Lưu seed + genome + dữ liệu/timerange + phiên bản Freqtrade để tái lập.

Đọc `docs/WORKFLOW.md` để xem quy trình chi tiết.
