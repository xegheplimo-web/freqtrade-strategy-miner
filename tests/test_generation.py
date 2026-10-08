from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from strategy_miner.compiler import compile_strategy  # noqa: E402
from strategy_miner.generator import GenomeGenerator  # noqa: E402


class GenerationTests(unittest.TestCase):
    def test_deterministic_generation(self) -> None:
        a = GenomeGenerator(seed=42).generate_one(1)
        b = GenomeGenerator(seed=42).generate_one(1)
        self.assertEqual(a, b)

    def test_compiled_strategy_is_valid_python(self) -> None:
        genome = GenomeGenerator(seed=42).generate_one(1)
        source = compile_strategy(genome)
        ast.parse(source)
        self.assertIn(f"class {genome.class_name}(IStrategy):", source)
        self.assertNotIn("shift(-", source)

    def test_compiled_strategy_exposes_buy_space_params(self) -> None:
        genome = GenomeGenerator(seed=42).generate_one(1)
        source = compile_strategy(genome)
        self.assertIn(
            f'IntParameter(20, 45, default={genome.rsi_long_max}, space="buy")',
            source,
        )
        self.assertIn(
            f'IntParameter(55, 80, default={genome.rsi_short_min}, space="buy")',
            source,
        )
        self.assertIn("self.buy_rsi_long_max.value", source)
        self.assertIn("self.buy_rsi_short_min.value", source)


if __name__ == "__main__":
    unittest.main()
