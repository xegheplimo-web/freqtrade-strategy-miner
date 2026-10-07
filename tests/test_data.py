"""Unit tests for ``strategy_miner.data`` (pure logic, pyarrow fixtures only)."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.feather as feather

from strategy_miner.data import (
    DataAudit,
    PairAudit,
    audit_data,
    data_checksums,
    download_args,
    feather_filename,
    pair_data_path,
)

PAIR = "BTC/USDT:USDT"
MISSING_PAIR = "ETH/USDT:USDT"
TIMEFRAME = "5m"
START = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)


def _base_rows(n: int = 100) -> list[dict]:
    """Return ``n`` clean 5-minute candles starting 2025-01-01 00:00 UTC."""
    rows: list[dict] = []
    for i in range(n):
        price = 100.0 + i
        rows.append(
            {
                "date": START + timedelta(minutes=5 * i),
                "open": price,
                "high": price + 1.0,
                "low": price - 1.0,
                "close": price + 0.5,
                "volume": 10.0 + i,
            }
        )
    return rows


def _write_feather(path: Path, rows: list[dict], *, tz_aware: bool = True) -> Path:
    """Write ``rows`` to a feather file at ``path`` (creating parent dirs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    date_type = pa.timestamp("ms", tz="UTC") if tz_aware else pa.timestamp("ms")
    dates = [row["date"] for row in rows]
    if not tz_aware:
        dates = [date.replace(tzinfo=None) for date in dates]
    table = pa.table(
        {
            "date": pa.array(dates, type=date_type),
            "open": pa.array([row["open"] for row in rows], type=pa.float64()),
            "high": pa.array([row["high"] for row in rows], type=pa.float64()),
            "low": pa.array([row["low"] for row in rows], type=pa.float64()),
            "close": pa.array([row["close"] for row in rows], type=pa.float64()),
            "volume": pa.array([row["volume"] for row in rows], type=pa.float64()),
        }
    )
    feather.write_feather(table, path)
    return path


def _data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def test_feather_filename_futures() -> None:
    assert feather_filename(PAIR, "5m") == "BTC_USDT_USDT-5m-futures.feather"
    assert feather_filename(PAIR, "15m") == "BTC_USDT_USDT-15m-futures.feather"
    assert feather_filename(PAIR, "1h") == "BTC_USDT_USDT-1h-futures.feather"


def test_feather_filename_spot() -> None:
    assert feather_filename(PAIR, "5m", trading_mode="spot") == "BTC_USDT_USDT-5m.feather"


def test_pair_data_path_futures(tmp_path: Path) -> None:
    path = pair_data_path(tmp_path / "data", PAIR, "5m")
    assert isinstance(path, Path)
    assert path == tmp_path / "data" / "binance" / "futures" / "BTC_USDT_USDT-5m-futures.feather"


def test_pair_data_path_spot(tmp_path: Path) -> None:
    path = pair_data_path(tmp_path / "data", PAIR, "5m", trading_mode="spot")
    assert path == tmp_path / "data" / "binance" / "BTC_USDT_USDT-5m.feather"


def test_download_args_exact_order() -> None:
    args = download_args(
        "/freqtrade/user_data/config.miner.json",
        [PAIR, MISSING_PAIR],
        ["5m", "1h"],
        "20220101-20241231",
    )
    assert args == [
        "download-data",
        "--config",
        "/freqtrade/user_data/config.miner.json",
        "--exchange",
        "binance",
        "--pairs",
        PAIR,
        MISSING_PAIR,
        "--timeframes",
        "5m",
        "1h",
        "--timerange",
        "20220101-20241231",
    ]
    assert "--trading-mode" not in args


def test_audit_happy_path(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    _write_feather(pair_data_path(data_dir, PAIR, TIMEFRAME), _base_rows(100))

    audit = audit_data(data_dir, [PAIR], [TIMEFRAME])
    assert isinstance(audit, DataAudit)
    assert audit.ok is True
    assert audit.issues == ()
    assert audit.timerange is None
    assert audit.timeframes == (TIMEFRAME,)
    assert len(audit.pairs) == 1

    entry = audit.pairs[0]
    assert isinstance(entry, PairAudit)
    assert entry.missing is False
    assert entry.rows == 100
    assert entry.duplicates == 0
    assert entry.gaps == 0
    assert entry.max_gap_minutes == 5.0
    assert entry.ohlc_violations == 0
    assert entry.zero_volume_rows == 0
    assert entry.first_ts == "2025-01-01T00:00:00+00:00"
    assert entry.last_ts == (START + timedelta(minutes=5 * 99)).isoformat()


def test_audit_injected_problems(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    rows = _base_rows(100)
    rows.append(dict(rows[10]))  # duplicate timestamp
    rows[20]["high"] = 5.0  # high < low and high < max(open, close)
    rows[20]["low"] = 50.0
    rows[30]["volume"] = 0.0  # zero-volume row
    rows = [row for idx, row in enumerate(rows) if idx not in (50, 51)]  # 15-min gap
    _write_feather(pair_data_path(data_dir, PAIR, TIMEFRAME), rows)

    audit = audit_data(data_dir, [PAIR], [TIMEFRAME])
    entry = audit.pairs[0]
    assert audit.ok is False
    assert audit.issues != ()
    assert entry.rows == 99
    assert entry.duplicates == 1
    assert entry.gaps == 1
    assert entry.max_gap_minutes == 15.0
    assert entry.ohlc_violations == 1
    assert entry.zero_volume_rows == 1


def test_audit_missing_pair(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)

    audit = audit_data(data_dir, [PAIR], [TIMEFRAME])
    assert audit.ok is False
    assert len(audit.pairs) == 1
    entry = audit.pairs[0]
    assert entry.missing is True
    assert entry.path == ""
    assert entry.rows == 0
    assert entry.first_ts == ""
    assert entry.last_ts == ""
    assert audit.issues != ()


def test_audit_timerange_spanning_beyond_file_is_issue(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    _write_feather(pair_data_path(data_dir, PAIR, TIMEFRAME), _base_rows(100))

    audit = audit_data(data_dir, [PAIR], [TIMEFRAME], timerange="20240101-")
    assert audit.timerange == "20240101-"
    assert audit.ok is False
    assert any("coverage" in issue for issue in audit.issues)


def test_audit_timerange_matching_passes(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    _write_feather(pair_data_path(data_dir, PAIR, TIMEFRAME), _base_rows(100))

    audit = audit_data(data_dir, [PAIR], [TIMEFRAME], timerange="20250101-")
    assert audit.ok is True
    assert audit.issues == ()


def test_audit_accepts_tz_naive_dates(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    _write_feather(pair_data_path(data_dir, PAIR, TIMEFRAME), _base_rows(100), tz_aware=False)

    audit = audit_data(data_dir, [PAIR], [TIMEFRAME])
    assert audit.ok is True
    assert audit.pairs[0].rows == 100
    assert audit.pairs[0].first_ts == "2025-01-01T00:00:00+00:00"


def test_data_checksums_deterministic_hex_and_skip_missing(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    path = _write_feather(pair_data_path(data_dir, PAIR, TIMEFRAME), _base_rows(100))

    first = data_checksums(data_dir, [PAIR, MISSING_PAIR], [TIMEFRAME])
    second = data_checksums(data_dir, [PAIR, MISSING_PAIR], [TIMEFRAME])

    assert first == second
    assert set(first) == {"binance/futures/BTC_USDT_USDT-5m-futures.feather"}
    digest = first["binance/futures/BTC_USDT_USDT-5m-futures.feather"]
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
