from __future__ import annotations

import json
import random
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from strategy_miner.params import (  # noqa: E402
    load_hyperopt_show_json,
    perturb_params,
    read_params_file,
    write_params_file,
)


def _sample_params() -> dict:
    return {
        "buy": {"buy_rsi": 30, "buy_ema": 12.0},
        "stoploss": -0.1,
        "minimal_roi": {"0": 0.1, "60": 0.05},
        "enabled": True,
        "note": None,
        "name": "sample",
        "list_vals": [10, 20.5, False, "s", None],
    }


def _numeric_pairs(orig, new):
    """Yield (original, perturbed) numeric leaves from parallel structures."""
    if isinstance(orig, dict):
        for key, value in orig.items():
            yield from _numeric_pairs(value, new[key])
    elif isinstance(orig, list):
        for o_item, n_item in zip(orig, new):
            yield from _numeric_pairs(o_item, n_item)
    elif isinstance(orig, bool) or orig is None or isinstance(orig, str):
        return
    elif isinstance(orig, (int, float)):
        yield orig, new


def test_write_params_file_structure_and_roundtrip(tmp_path: Path) -> None:
    strategy_py = tmp_path / "Miner_000001.py"
    strategy_py.write_text("class Miner_000001:\n    pass\n", encoding="utf-8")
    params = {"buy": {"buy_rsi": 30}, "stoploss": -0.1, "minimal_roi": {"0": 0.1}}

    out = write_params_file(strategy_py, params, strategy_name="Miner_000001")

    assert out == strategy_py.with_suffix(".json")
    assert out.exists()
    assert out.suffix == ".json"
    assert out.parent == strategy_py.parent

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ft_stratparam_v"] == 1
    assert data["strategy_name"] == "Miner_000001"
    datetime.fromisoformat(data["export_time"])  # parseable ISO-8601
    assert read_params_file(out) == params


def test_perturb_deterministic_for_seed() -> None:
    a = perturb_params(_sample_params(), 0.1, rng=random.Random(123))
    b = perturb_params(_sample_params(), 0.1, rng=random.Random(123))
    c = perturb_params(_sample_params(), 0.1, rng=random.Random(999))
    assert a == b
    assert c != a


def test_perturb_numeric_within_pct() -> None:
    pct = 0.2
    orig = _sample_params()
    new = perturb_params(orig, pct, rng=random.Random(7))
    pairs = list(_numeric_pairs(orig, new))
    assert pairs  # sanity: numeric leaves were walked
    for original, perturbed in pairs:
        if isinstance(original, int):
            assert isinstance(perturbed, int)
            assert perturbed >= 1
            assert abs(perturbed - original) <= original * pct + 1
        else:
            assert isinstance(perturbed, float)
            assert abs(perturbed - original) <= abs(original) * pct + 1e-4


def test_perturb_non_numeric_unchanged() -> None:
    orig = {"enabled": True, "note": None, "name": "sample"}
    new = perturb_params(orig, 0.5, rng=random.Random(1))
    assert new["enabled"] is True
    assert new["note"] is None
    assert new["name"] == "sample"
    assert new == orig


def test_perturb_walks_nested_dict_and_list() -> None:
    orig = _sample_params()
    new = perturb_params(orig, 0.3, rng=random.Random(5))
    assert set(new) == set(orig)
    assert set(new["buy"]) == set(orig["buy"])
    assert set(new["minimal_roi"]) == set(orig["minimal_roi"])
    assert len(new["list_vals"]) == len(orig["list_vals"])
    assert new["list_vals"][2] is False
    assert new["list_vals"][3] == "s"
    assert new["list_vals"][4] is None
    # nested positive int stays a positive int
    assert isinstance(new["buy"]["buy_rsi"], int)
    assert new["buy"]["buy_rsi"] >= 1


def test_load_hyperopt_show_json_parses_last_json_line() -> None:
    text = (
        "Some header\n"
        "Epoch details\n"
        '{"params": {"buy_rsi": 30}, "stoploss": -0.1, "minimal_roi": {"0": 0.1}}\n'
    )
    expected = {"params": {"buy_rsi": 30}, "stoploss": -0.1, "minimal_roi": {"0": 0.1}}
    assert load_hyperopt_show_json(text) == expected


def test_load_hyperopt_show_json_ignores_trailing_prose() -> None:
    text = (
        "header\n"
        '{"params": {"buy_rsi": 30}}\n'
        "Best epoch stored.\n"
    )
    assert load_hyperopt_show_json(text) == {"params": {"buy_rsi": 30}}


def test_load_hyperopt_show_json_garbage_raises() -> None:
    with pytest.raises(ValueError):
        load_hyperopt_show_json("no json here\nstill nothing\n")
    with pytest.raises(ValueError):
        load_hyperopt_show_json("")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
