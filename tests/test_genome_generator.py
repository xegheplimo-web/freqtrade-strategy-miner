from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from strategy_miner.compiler import compile_strategy  # noqa: E402
from strategy_miner.generator import GenomeGenerator  # noqa: E402
from strategy_miner.genome import StrategyGenome  # noqa: E402


def make_genome(**overrides) -> StrategyGenome:
    base = {
        "strategy_id": 1,
        "timeframe": "5m",
        "can_short": True,
        "ema_fast": 10,
        "ema_slow": 30,
        "rsi_period": 14,
        "rsi_long_max": 30,
        "rsi_short_min": 70,
        "stoploss": -0.1,
    }
    base.update(overrides)
    return StrategyGenome(**base)


class ValidateRejectionTests(unittest.TestCase):
    def test_valid_genome_passes(self) -> None:
        make_genome().validate()

    def test_ema_fast_equal_slow_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_genome(ema_fast=30, ema_slow=30).validate()

    def test_ema_fast_greater_than_slow_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_genome(ema_fast=40, ema_slow=30).validate()

    def test_rsi_period_low_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_genome(rsi_period=1).validate()

    def test_rsi_period_high_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_genome(rsi_period=101).validate()

    def test_rsi_long_max_zero_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_genome(rsi_long_max=0).validate()

    def test_rsi_long_max_fifty_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_genome(rsi_long_max=50).validate()

    def test_rsi_short_min_fifty_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_genome(rsi_short_min=50).validate()

    def test_rsi_short_min_hundred_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_genome(rsi_short_min=100).validate()

    def test_stoploss_zero_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_genome(stoploss=0.0).validate()

    def test_stoploss_minus_one_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_genome(stoploss=-1.0).validate()

    def test_stoploss_positive_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_genome(stoploss=0.1).validate()


class GeneratorRangeTests(unittest.TestCase):
    def test_many_seeds_validate_and_respect_ranges(self) -> None:
        for seed in range(100):
            gen = GenomeGenerator(seed=seed)
            genomes = gen.generate_many(5)
            self.assertEqual(len(genomes), 5)
            for genome in genomes:
                with self.subTest(seed=seed, genome=genome):
                    genome.validate()
                    self.assertGreaterEqual(genome.ema_fast, 5)
                    self.assertLessEqual(genome.ema_fast, 50)
                    self.assertGreaterEqual(genome.ema_slow, genome.ema_fast + 10)
                    self.assertGreaterEqual(genome.rsi_long_max, 22)
                    self.assertLessEqual(genome.rsi_long_max, 45)
                    self.assertGreaterEqual(genome.rsi_short_min, 55)
                    self.assertLessEqual(genome.rsi_short_min, 78)

    def test_same_seed_produces_equal_sequences(self) -> None:
        first = GenomeGenerator(seed=7).generate_many(5)
        second = GenomeGenerator(seed=7).generate_many(5)
        self.assertEqual(first, second)

    def test_different_seeds_produce_different_sequences(self) -> None:
        first = GenomeGenerator(seed=1).generate_many(5)
        second = GenomeGenerator(seed=2).generate_many(5)
        self.assertNotEqual(first, second)


class CompilerOutputTests(unittest.TestCase):
    def test_compiled_output_for_generated_genomes(self) -> None:
        genomes: list[StrategyGenome] = []
        seed = 0
        while len(genomes) < 50:
            genomes.extend(GenomeGenerator(seed=seed).generate_many(5))
            seed += 1
        genomes = genomes[:50]
        for genome in genomes:
            with self.subTest(genome=genome):
                source = compile_strategy(genome)
                ast.parse(source)
                self.assertIn(f"class {genome.class_name}(IStrategy):", source)
                self.assertNotIn("shift(-", source)
                match = re.search(r"startup_candle_count\s*=\s*(\d+)", source)
                self.assertIsNotNone(match)
                assert match is not None
                self.assertGreaterEqual(int(match.group(1)), genome.ema_slow + 20)


if __name__ == "__main__":
    unittest.main()
