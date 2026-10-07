"""SQLite registry for mined strategy candidates, runs and stage progress.

Backed by the stdlib :mod:`sqlite3` (no third-party deps). The database lives at
``<root>/output/miner.db`` (gitignored). ``PRAGMA foreign_keys=ON`` is enabled on
every connection. All timestamps are stored as UTC ISO-8601 strings and all JSON
payloads use ``json.dumps(..., ensure_ascii=False, sort_keys=True)``.

Frozen vocabularies (INTERFACES.md §3):

- Stage names: ``fast_gate``, ``hyperopt``, ``validation``, ``bias``,
  ``walk_forward``, ``robustness``, ``final_test``.
- Stage statuses: ``pending``, ``running``, ``passed``, ``failed``,
  ``shortlisted``, ``rejected``, ``skipped``.

Schema (must exist):

- ``candidates(id INTEGER PRIMARY KEY, strategy_id INT, class_name TEXT UNIQUE,
  genome_json TEXT, py_path TEXT, params_path TEXT, created_at TEXT)``
- ``runs(id INTEGER PRIMARY KEY, candidate_id INT REFERENCES candidates(id),
  stage TEXT, argv_json TEXT, exit_code INT, duration_s REAL, artifact_path TEXT,
  metrics_json TEXT, notes TEXT, created_at TEXT)``
- ``stages(candidate_id INT REFERENCES candidates(id), stage TEXT, status TEXT,
  note TEXT, updated_at TEXT, PRIMARY KEY(candidate_id, stage))``
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .genome import StrategyGenome

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    strategy_id INT,
    class_name TEXT UNIQUE,
    genome_json TEXT,
    py_path TEXT,
    params_path TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    candidate_id INT REFERENCES candidates(id),
    stage TEXT,
    argv_json TEXT,
    exit_code INT,
    duration_s REAL,
    artifact_path TEXT,
    metrics_json TEXT,
    notes TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS stages (
    candidate_id INT REFERENCES candidates(id),
    stage TEXT,
    status TEXT,
    note TEXT,
    updated_at TEXT,
    PRIMARY KEY (candidate_id, stage)
);
"""


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class Store:
    """Thin registry over a single SQLite database file."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    # -- candidates ---------------------------------------------------------

    def upsert_candidate(
        self, genome: StrategyGenome, py_path: str, params_path: str | None = None
    ) -> int:
        """Insert or update a candidate keyed by ``genome.class_name``.

        Returns the candidate id. On a repeat call with the same class name the
        stored paths (and genome) are updated while ``created_at`` is preserved.
        """
        class_name = genome.class_name
        genome_json = json.dumps(genome.to_dict(), ensure_ascii=False, sort_keys=True)
        self._conn.execute(
            """
            INSERT INTO candidates
                (strategy_id, class_name, genome_json, py_path, params_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(class_name) DO UPDATE SET
                strategy_id=excluded.strategy_id,
                genome_json=excluded.genome_json,
                py_path=excluded.py_path,
                params_path=excluded.params_path
            """,
            (
                genome.strategy_id,
                class_name,
                genome_json,
                str(py_path),
                None if params_path is None else str(params_path),
                _utc_now_iso(),
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM candidates WHERE class_name = ?", (class_name,)
        ).fetchone()
        return int(row["id"])

    def get_candidate(self, candidate_id: int) -> dict[str, Any]:
        """Return the candidate row as a dict; raise ``KeyError`` if missing."""
        row = self._conn.execute(
            "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return dict(row)

    def list_candidates(self) -> list[dict[str, Any]]:
        """Return all candidates ordered by ``strategy_id``."""
        rows = self._conn.execute(
            "SELECT * FROM candidates ORDER BY strategy_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def candidate_by_class(self, class_name: str) -> dict[str, Any] | None:
        """Return the candidate with this class name, or ``None``."""
        row = self._conn.execute(
            "SELECT * FROM candidates WHERE class_name = ?", (class_name,)
        ).fetchone()
        return None if row is None else dict(row)

    # -- runs ---------------------------------------------------------------

    def record_run(
        self,
        candidate_id: int,
        stage: str,
        argv: Sequence[str],
        exit_code: int,
        duration_s: float,
        artifact_path: str | None = None,
        metrics: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> int:
        """Record a stage run for a candidate; returns the new run id.

        Raises ``ValueError`` when ``candidate_id`` is unknown.
        """
        known = self._conn.execute(
            "SELECT 1 FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if known is None:
            raise ValueError(f"unknown candidate_id: {candidate_id}")

        argv_json = json.dumps(list(argv), ensure_ascii=False, sort_keys=True)
        metrics_json = (
            None
            if metrics is None
            else json.dumps(metrics, ensure_ascii=False, sort_keys=True)
        )
        cursor = self._conn.execute(
            """
            INSERT INTO runs
                (candidate_id, stage, argv_json, exit_code, duration_s,
                 artifact_path, metrics_json, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                stage,
                argv_json,
                int(exit_code),
                float(duration_s),
                None if artifact_path is None else str(artifact_path),
                metrics_json,
                notes,
                _utc_now_iso(),
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def runs_for(
        self, candidate_id: int, stage: str | None = None
    ) -> list[dict[str, Any]]:
        """Return runs for a candidate ordered by id, optionally filtered by stage.

        ``argv_json``/``metrics_json`` are decoded back into a native list/dict
        (``metrics`` is ``None`` when the run stored no metrics).
        """
        if stage is None:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE candidate_id = ? ORDER BY id",
                (candidate_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE candidate_id = ? AND stage = ? ORDER BY id",
                (candidate_id, stage),
            ).fetchall()

        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["argv"] = json.loads(item.pop("argv_json"))
            raw_metrics = item.pop("metrics_json")
            item["metrics"] = None if raw_metrics is None else json.loads(raw_metrics)
            result.append(item)
        return result

    # -- stage status -------------------------------------------------------

    def set_stage_status(
        self, candidate_id: int, stage: str, status: str, note: str | None = None
    ) -> None:
        """Upsert the status of a stage for a candidate."""
        self._conn.execute(
            """
            INSERT INTO stages (candidate_id, stage, status, note, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id, stage) DO UPDATE SET
                status=excluded.status,
                note=excluded.note,
                updated_at=excluded.updated_at
            """,
            (candidate_id, stage, status, note, _utc_now_iso()),
        )
        self._conn.commit()

    def stage_status(self, candidate_id: int, stage: str) -> str | None:
        """Return the stored status for a stage, or ``None`` when unset."""
        row = self._conn.execute(
            "SELECT status FROM stages WHERE candidate_id = ? AND stage = ?",
            (candidate_id, stage),
        ).fetchone()
        return None if row is None else str(row["status"])

    def shortlist(self) -> list[int]:
        """Return ids with stage ``fast_gate`` and status ``shortlisted``, ordered."""
        rows = self._conn.execute(
            "SELECT candidate_id FROM stages WHERE stage = ? AND status = ? "
            "ORDER BY candidate_id",
            ("fast_gate", "shortlisted"),
        ).fetchall()
        return [int(row["candidate_id"]) for row in rows]
