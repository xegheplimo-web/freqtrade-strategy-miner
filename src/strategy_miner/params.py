"""Hyperopt params flow: freqtrade params-file write/read, perturbation and parsing.

Freqtrade auto-exports the best hyperopt parameters to a ``<strategy>.py.json``
sidecar next to the strategy file (see INTERFACES.md §4). This module writes and
reads that native format, perturbs numeric parameter trees for robustness checks,
and parses the single JSON line printed by ``freqtrade hyperopt-show --best
--print-json``.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def write_params_file(strategy_py: Path, params: dict[str, Any], *, strategy_name: str) -> Path:
    """Write a freqtrade-native params sidecar next to ``strategy_py``.

    The file is written as ``<strategy_py>.json`` (``with_suffix(".json")``) with
    the exact structure ``{"strategy_name": ..., "params": params,
    "ft_stratparam_v": 1, "export_time": <UTC ISO>}``. Returns the written path.
    """
    path = Path(strategy_py).with_suffix(".json")
    payload = {
        "strategy_name": strategy_name,
        "params": params,
        "ft_stratparam_v": 1,
        "export_time": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=float),
        encoding="utf-8",
    )
    return path


def read_params_file(path: Path) -> dict[str, Any]:
    """Return the inner ``"params"`` dict of a params sidecar."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["params"]


def perturb_params(
    params: dict[str, Any], pct: float, *, rng: random.Random
) -> dict[str, Any]:
    """Deep-copy ``params`` while perturbing numeric leaves by ``+/- pct``.

    Rules (INTERFACES.md §4): ``bool``/``None``/``str`` are unchanged; a positive
    ``int`` becomes ``max(1, int(round(v * (1 + delta))))``; other ints are
    unchanged; a ``float`` becomes ``round(v * (1 + delta), 4)``; ``dict``/``list``
    recurse; ``delta = rng.uniform(-pct, +pct)``. Deterministic for a seeded rng.
    """
    return _perturb(params, pct, rng)


def _perturb(value: Any, pct: float, rng: random.Random) -> Any:
    if isinstance(value, bool):
        return value
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        if value > 0:
            delta = rng.uniform(-pct, pct)
            return max(1, int(round(value * (1 + delta))))
        return value
    if isinstance(value, float):
        delta = rng.uniform(-pct, pct)
        return round(value * (1 + delta), 4)
    if isinstance(value, dict):
        return {key: _perturb(item, pct, rng) for key, item in value.items()}
    if isinstance(value, list):
        return [_perturb(item, pct, rng) for item in value]
    return value


def load_hyperopt_show_json(text: str) -> dict[str, Any]:
    """Parse the last line of ``hyperopt-show --best --print-json`` stdout.

    Freqtrade prints prose headers followed by a single JSON object line. Scans
    the lines from the end and returns the last one that ``json.loads()`` into a
    dict. Raises ``ValueError`` when no such line exists.
    """
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("no JSON object found in hyperopt-show output")
