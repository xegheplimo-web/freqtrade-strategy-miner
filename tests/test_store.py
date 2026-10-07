from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from strategy_miner.genome import StrategyGenome  # noqa: E402
from strategy_miner.store import Store  # noqa: E402


def _genome(strategy_id: int) -> StrategyGenome:
    return StrategyGenome(
        strategy_id=strategy_id,
        timeframe="5m",
        can_short=False,
        ema_fast=10,
        ema_slow=30,
        rsi_period=14,
        rsi_long_max=30,
        rsi_short_min=70,
        stoploss=-0.1,
    )


def test_schema_created_on_init(tmp_path: Path) -> None:
    db_path = tmp_path / "miner.db"
    Store(db_path)
    assert db_path.exists()
    con = sqlite3.connect(str(db_path))
    try:
        names = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        con.close()
    assert {"candidates", "runs", "stages"} <= names


def test_upsert_same_class_same_id_updates_paths(tmp_path: Path) -> None:
    store = Store(tmp_path / "miner.db")
    genome = _genome(1)
    first_id = store.upsert_candidate(genome, "gen/Miner_000001.py")
    second_id = store.upsert_candidate(
        genome, "gen/Miner_000001.py", params_path="gen/Miner_000001.json"
    )
    assert first_id == second_id
    candidate = store.get_candidate(first_id)
    assert candidate["py_path"] == "gen/Miner_000001.py"
    assert candidate["params_path"] == "gen/Miner_000001.json"


def test_list_candidates_ordered_by_strategy_id(tmp_path: Path) -> None:
    store = Store(tmp_path / "miner.db")
    for strategy_id in (3, 1, 2):
        store.upsert_candidate(_genome(strategy_id), f"gen/Miner_{strategy_id:06d}.py")
    ids = [row["strategy_id"] for row in store.list_candidates()]
    assert ids == [1, 2, 3]


def test_get_candidate_missing_raises_keyerror(tmp_path: Path) -> None:
    store = Store(tmp_path / "miner.db")
    with pytest.raises(KeyError):
        store.get_candidate(999)


def test_candidate_by_class_hit_and_miss(tmp_path: Path) -> None:
    store = Store(tmp_path / "miner.db")
    store.upsert_candidate(_genome(1), "gen/Miner_000001.py")
    assert store.candidate_by_class("Miner_999999") is None
    found = store.candidate_by_class("Miner_000001")
    assert found is not None
    assert found["strategy_id"] == 1


def test_record_run_increasing_ids(tmp_path: Path) -> None:
    store = Store(tmp_path / "miner.db")
    cid = store.upsert_candidate(_genome(1), "gen/Miner_000001.py")
    first = store.record_run(cid, "fast_gate", ["backtesting"], 0, 1.5)
    second = store.record_run(cid, "hyperopt", ["hyperopt"], 0, 2.5)
    assert second > first


def test_runs_for_ordering_and_stage_filter(tmp_path: Path) -> None:
    store = Store(tmp_path / "miner.db")
    cid = store.upsert_candidate(_genome(1), "gen/Miner_000001.py")
    r1 = store.record_run(cid, "fast_gate", ["a"], 0, 1.0)
    r2 = store.record_run(cid, "hyperopt", ["b"], 0, 1.0)
    r3 = store.record_run(cid, "fast_gate", ["c"], 0, 1.0)

    all_runs = store.runs_for(cid)
    assert [run["id"] for run in all_runs] == [r1, r2, r3]

    gate_runs = store.runs_for(cid, stage="fast_gate")
    assert [run["id"] for run in gate_runs] == [r1, r3]
    assert all(run["stage"] == "fast_gate" for run in gate_runs)


def test_metrics_round_trip(tmp_path: Path) -> None:
    store = Store(tmp_path / "miner.db")
    cid = store.upsert_candidate(_genome(1), "gen/Miner_000001.py")
    store.record_run(
        cid, "fast_gate", ["a"], 0, 1.0, metrics={"sharpe": 1.5, "trades": 10}
    )
    run = store.runs_for(cid)[0]
    assert run["metrics"] == {"sharpe": 1.5, "trades": 10}
    assert run["argv"] == ["a"]


def test_record_run_unknown_candidate_raises(tmp_path: Path) -> None:
    store = Store(tmp_path / "miner.db")
    with pytest.raises(ValueError):
        store.record_run(123, "fast_gate", ["a"], 0, 1.0)


def test_set_stage_status_upsert(tmp_path: Path) -> None:
    store = Store(tmp_path / "miner.db")
    cid = store.upsert_candidate(_genome(1), "gen/Miner_000001.py")
    store.set_stage_status(cid, "fast_gate", "running")
    assert store.stage_status(cid, "fast_gate") == "running"
    store.set_stage_status(cid, "fast_gate", "shortlisted", note="kept")
    assert store.stage_status(cid, "fast_gate") == "shortlisted"
    assert store.stage_status(cid, "hyperopt") is None


def test_shortlist_only_shortlisted_fast_gate(tmp_path: Path) -> None:
    store = Store(tmp_path / "miner.db")
    c1 = store.upsert_candidate(_genome(1), "gen/Miner_000001.py")
    c2 = store.upsert_candidate(_genome(2), "gen/Miner_000002.py")
    c3 = store.upsert_candidate(_genome(3), "gen/Miner_000003.py")

    store.set_stage_status(c1, "fast_gate", "shortlisted")
    store.set_stage_status(c2, "fast_gate", "rejected")
    store.set_stage_status(c3, "hyperopt", "shortlisted")  # wrong stage

    assert store.shortlist() == [c1]


def test_persistence_across_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "miner.db"
    first = Store(db_path)
    cid = first.upsert_candidate(_genome(1), "gen/Miner_000001.py")
    first.record_run(cid, "fast_gate", ["a"], 0, 1.0)
    first.set_stage_status(cid, "fast_gate", "shortlisted")
    first.close()

    reopened = Store(db_path)
    assert reopened.candidate_by_class("Miner_000001") is not None
    assert len(reopened.runs_for(cid)) == 1
    assert reopened.stage_status(cid, "fast_gate") == "shortlisted"
    assert reopened.shortlist() == [cid]
    reopened.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
