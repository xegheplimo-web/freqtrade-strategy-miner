"""Data stage: feather filenames, QA audit, download args, checksums.

Pure logic — no docker, no network. Candle history is read from ``.feather``
files (via pyarrow) laid out as ``<data_dir>/<exchange>/futures/<file>`` for
futures and ``<data_dir>/<exchange>/<file>`` for spot.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import pyarrow as pa
import pyarrow.feather as feather

__all__ = [
    "DataAudit",
    "PairAudit",
    "audit_data",
    "data_checksums",
    "download_args",
    "feather_filename",
    "pair_data_path",
]

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MS_PER_DAY = 86_400_000
_TIMEFRAME_MS = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
_CHUNK_SIZE = 1024 * 1024
# Timerange coverage tolerance: more than 5% of expected rows missing -> issue.
_MISSING_FRAC_LIMIT = 0.05
# Gap detection: a diff larger than 1.5x the expected interval is a gap.
_GAP_FACTOR = 1.5


@dataclass(frozen=True)
class PairAudit:
    pair: str
    timeframe: str
    rows: int
    first_ts: str
    last_ts: str
    duplicates: int
    gaps: int
    max_gap_minutes: float
    ohlc_violations: int
    zero_volume_rows: int
    path: str
    missing: bool


@dataclass(frozen=True)
class DataAudit:
    pairs: tuple[PairAudit, ...]
    timeframes: tuple[str, ...]
    timerange: str | None
    ok: bool
    issues: tuple[str, ...]


def feather_filename(pair: str, timeframe: str, *, trading_mode: str = "futures") -> str:
    """Return the freqtrade feather filename for ``pair``/``timeframe``.

    ``"BTC/USDT:USDT"`` + ``"5m"`` -> ``"BTC_USDT_USDT-5m-futures.feather"`` (futures)
    or ``"BTC_USDT_USDT-5m.feather"`` (spot).
    """
    stem = pair.replace("/", "_").replace(":", "_")
    if trading_mode == "futures":
        return f"{stem}-{timeframe}-futures.feather"
    return f"{stem}-{timeframe}.feather"


def pair_data_path(
    data_dir: Path,
    pair: str,
    timeframe: str,
    *,
    exchange: str = "binance",
    trading_mode: str = "futures",
) -> Path:
    """Return the on-disk path of a pair/timeframe feather file."""
    data_dir = Path(data_dir)
    filename = feather_filename(pair, timeframe, trading_mode=trading_mode)
    if trading_mode == "futures":
        return data_dir / exchange / "futures" / filename
    return data_dir / exchange / filename


def download_args(
    config_container_path: str,
    pairs: Sequence[str],
    timeframes: Sequence[str],
    timerange: str,
    *,
    exchange: str = "binance",
) -> list[str]:
    """Build the freqtrade ``download-data`` argv (container paths).

    Trading mode (futures) comes from the freqtrade config file, so no
    ``--trading-mode`` flag is emitted here.
    """
    return [
        "download-data",
        "--config",
        config_container_path,
        "--exchange",
        exchange,
        "--pairs",
        *pairs,
        "--timeframes",
        *timeframes,
        "--timerange",
        timerange,
    ]


def data_checksums(
    data_dir: Path,
    pairs: Sequence[str],
    timeframes: Sequence[str],
    *,
    exchange: str = "binance",
    trading_mode: str = "futures",
) -> dict[str, str]:
    """Return ``{relative posix path: sha256 hex}`` for existing feather files."""
    data_dir = Path(data_dir)
    checksums: dict[str, str] = {}
    for pair in pairs:
        for timeframe in timeframes:
            path = pair_data_path(
                data_dir, pair, timeframe, exchange=exchange, trading_mode=trading_mode
            )
            if not path.is_file():
                continue
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
                    digest.update(chunk)
            try:
                key = path.relative_to(data_dir).as_posix()
            except ValueError:
                key = path.name
            checksums[key] = digest.hexdigest()
    return checksums


def audit_data(
    data_dir: Path,
    pairs: Sequence[str],
    timeframes: Sequence[str],
    *,
    timerange: str | None = None,
    exchange: str = "binance",
    trading_mode: str = "futures",
) -> DataAudit:
    """Audit feather candle files for every (pair, timeframe) combination."""
    data_dir = Path(data_dir)
    audits: list[PairAudit] = []
    issues: list[str] = []
    ok = True

    for pair in pairs:
        for timeframe in timeframes:
            path = pair_data_path(
                data_dir, pair, timeframe, exchange=exchange, trading_mode=trading_mode
            )
            if not path.is_file():
                audits.append(_missing_audit(pair, timeframe))
                issues.append(f"{pair} {timeframe}: missing data file ({path.name})")
                ok = False
                continue

            table = feather.read_table(
                path, columns=["date", "open", "high", "low", "close", "volume"]
            )
            timestamps = sorted(_date_column_ms(table.column("date")))
            rows = len(timestamps)

            if rows == 0:
                audits.append(_empty_audit(pair, timeframe, str(path)))
                continue

            diffs = [timestamps[i] - timestamps[i - 1] for i in range(1, rows)]
            duplicates = sum(1 for diff in diffs if diff == 0)
            interval_ms = _timeframe_ms(timeframe)
            gaps = sum(1 for diff in diffs if diff > _GAP_FACTOR * interval_ms)
            max_gap_minutes = max(diffs) / 60_000.0 if diffs else 0.0
            ohlc_violations, zero_volume_rows = _count_row_problems(table, rows)

            bad = False
            if duplicates:
                issues.append(f"{pair} {timeframe}: {duplicates} duplicate timestamp(s)")
                bad = True
            if ohlc_violations:
                issues.append(f"{pair} {timeframe}: {ohlc_violations} OHLC violation(s)")
                bad = True
            if gaps:
                issues.append(f"{pair} {timeframe}: {gaps} gap(s), max {max_gap_minutes:.1f} min")
            if zero_volume_rows:
                issues.append(f"{pair} {timeframe}: {zero_volume_rows} zero-volume row(s)")

            if timerange:
                missing_frac = _missing_fraction(timerange, timestamps, interval_ms)
                if missing_frac > _MISSING_FRAC_LIMIT:
                    issues.append(
                        f"{pair} {timeframe}: timerange coverage "
                        f"missing_frac={missing_frac:.3f} > {_MISSING_FRAC_LIMIT}"
                    )
                    bad = True

            if bad:
                ok = False

            audits.append(
                PairAudit(
                    pair=pair,
                    timeframe=timeframe,
                    rows=rows,
                    first_ts=_ms_to_iso(timestamps[0]),
                    last_ts=_ms_to_iso(timestamps[-1]),
                    duplicates=duplicates,
                    gaps=gaps,
                    max_gap_minutes=max_gap_minutes,
                    ohlc_violations=ohlc_violations,
                    zero_volume_rows=zero_volume_rows,
                    path=str(path),
                    missing=False,
                )
            )

    return DataAudit(
        pairs=tuple(audits),
        timeframes=tuple(timeframes),
        timerange=timerange,
        ok=ok,
        issues=tuple(issues),
    )


def _missing_audit(pair: str, timeframe: str) -> PairAudit:
    return PairAudit(
        pair=pair,
        timeframe=timeframe,
        rows=0,
        first_ts="",
        last_ts="",
        duplicates=0,
        gaps=0,
        max_gap_minutes=0.0,
        ohlc_violations=0,
        zero_volume_rows=0,
        path="",
        missing=True,
    )


def _empty_audit(pair: str, timeframe: str, path: str) -> PairAudit:
    return PairAudit(
        pair=pair,
        timeframe=timeframe,
        rows=0,
        first_ts="",
        last_ts="",
        duplicates=0,
        gaps=0,
        max_gap_minutes=0.0,
        ohlc_violations=0,
        zero_volume_rows=0,
        path=path,
        missing=False,
    )


def _timeframe_ms(timeframe: str) -> int:
    unit = timeframe[-1:].lower()
    amount = timeframe[:-1]
    if unit not in _TIMEFRAME_MS or not amount.isdigit():
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    return int(amount) * _TIMEFRAME_MS[unit]


def _date_column_ms(column: pa.ChunkedArray) -> list[int]:
    """Normalize a ``date`` column to a list of UTC epoch milliseconds."""
    if pa.types.is_timestamp(column.type):
        # Cast to ms/UTC: tz-naive values are treated as UTC, tz-aware keep the instant.
        casted = column.cast(pa.timestamp("ms", tz="UTC"))
        return [int(value) for value in casted.cast(pa.int64()).to_pylist()]
    if pa.types.is_integer(column.type):
        return [int(value) for value in column.to_pylist()]
    raise ValueError(f"unsupported 'date' column type: {column.type}")


def _count_row_problems(table: pa.Table, rows: int) -> tuple[int, int]:
    opens = table.column("open").to_pylist()
    highs = table.column("high").to_pylist()
    lows = table.column("low").to_pylist()
    closes = table.column("close").to_pylist()
    volumes = table.column("volume").to_pylist()

    ohlc_violations = 0
    zero_volume_rows = 0
    for i in range(rows):
        high = highs[i]
        low = lows[i]
        if high < max(opens[i], closes[i]) or low > min(opens[i], closes[i]) or high < low:
            ohlc_violations += 1
        if volumes[i] == 0:
            zero_volume_rows += 1
    return ohlc_violations, zero_volume_rows


def _ms_to_iso(ms: int) -> str:
    return (_EPOCH + timedelta(milliseconds=ms)).isoformat()


def _parse_ymd(day: str) -> int:
    parsed = datetime.strptime(day.strip(), "%Y%m%d").replace(tzinfo=timezone.utc)
    return int((parsed - _EPOCH).total_seconds() * 1000)


def _timerange_bounds(timerange: str, first_ms: int, last_ms: int) -> tuple[int, int]:
    start_raw, _, end_raw = timerange.partition("-")
    low = _parse_ymd(start_raw) if start_raw else first_ms
    high = (_parse_ymd(end_raw) + _MS_PER_DAY - 1) if end_raw else last_ms
    return low, high


def _missing_fraction(timerange: str, timestamps: list[int], interval_ms: int) -> float:
    low, high = _timerange_bounds(timerange, timestamps[0], timestamps[-1])
    if high < low:
        return 0.0
    expected = (high - low) // interval_ms + 1
    if expected <= 0:
        return 0.0
    actual = sum(1 for ts in timestamps if low <= ts <= high)
    return max(0.0, (expected - actual) / expected)
