# Workflow chi tiết

## Stage 0 — Data

1. Download dữ liệu Futures cho các pair cần test.
2. Kiểm tra missing candles, duplicate timestamp và khoảng thời gian của từng pair.
3. Khóa ba vùng: TRAIN, VALIDATION, FINAL_TEST.
4. Ghi checksum/version của dataset vào experiment manifest.

## Stage 1 — Generate

`generator.py` sinh Genome với các nhóm:

- trend: EMA fast / EMA slow
- momentum: RSI
- volume confirmation
- stoploss cơ bản
- long/short capability

Starter template cố tình giới hạn grammar. Sau khi pipeline đúng mới mở rộng Supertrend, ATR, BBands, ADX, market regime, informative pairs...

## Stage 2 — Compile

`compiler.py` chuyển Genome thành một class `IStrategy` độc lập.

Yêu cầu:

- deterministic từ genome
- tên class duy nhất
- không dùng negative shift
- không dùng dữ liệu candle tương lai
- luôn có volume guard

## Stage 3 — Fast gate

Backtest TRAIN tất cả candidate.

Hard reject gợi ý:

- trades < min_trades
- max_drawdown > max_drawdown
- profit_factor < min_profit_factor
- expectancy <= 0
- quá phụ thuộc một pair

Chỉ top 1–5% đi tiếp.

## Stage 4 — Hyperopt

Hyperopt không phải Strategy Miner. Hyperopt chỉ tinh chỉnh candidate đã có cấu trúc tốt.

Thứ tự gợi ý:

1. entry/exit parameters
2. ROI + stoploss
3. protections nếu dùng
4. trailing chạy riêng

Giữ TRAIN cố định trong giai đoạn này.

## Stage 5 — Validation

Chạy candidate tối ưu trên VALIDATION chưa dùng để tối ưu.

Reject nếu:

- drawdown tăng đột biến
- profit factor sụp mạnh
- expectancy âm
- lợi nhuận tập trung vào quá ít trade/pair
- parameter neighborhood không ổn định

## Stage 6 — Bias checks

Chạy Freqtrade `lookahead-analysis` và `recursive-analysis` trên candidate còn sống.

Bất kỳ lookahead bias nào cũng là hard reject.

## Stage 7 — Walk-forward

Ví dụ:

```text
Train 12 tháng → test 3 tháng
cuộn cửa sổ 3 tháng → lặp lại
```

Tổng hợp median/dispersion giữa các fold thay vì chỉ nhìn fold tốt nhất.

## Stage 8 — Robustness

Stress candidate:

- tăng fee
- giả lập slippage
- delay entry
- perturb parameter ±5–10%
- bỏ top-N trades
- test nhiều pair
- test các market regime khác nhau

Mục tiêu là tìm plateau ổn định, không phải một điểm parameter cực đại duy nhất.

## Stage 9 — Final Test

Chỉ những strategy đã freeze mới được chạy FINAL_TEST.

Nếu xem Final Test rồi sửa rule/parameter thì candidate mới phải được coi là một experiment mới.

## Stage 10 — Promote

`Champion → dry-run → so khớp execution → live vốn nhỏ → scale`

Không để pipeline tự chuyển sang live nếu không có explicit approval.
