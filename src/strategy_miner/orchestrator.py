from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .commands import backtest_command, hyperopt_command, lookahead_command, recursive_command
from .compiler import write_strategy
from .generator import GenomeGenerator


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / "config" / "miner.json"

    @property
    def freqtrade_config(self) -> Path:
        actual = self.root / "freqtrade" / "config.json"
        return actual if actual.exists() else self.root / "freqtrade" / "config.example.json"

    @property
    def generated(self) -> Path:
        return self.root / "freqtrade" / "user_data" / "strategies" / "generated"

    @property
    def manifests(self) -> Path:
        return self.root / "output" / "manifests"


def load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def generate_batch(root: Path, count: int | None = None) -> dict:
    paths = Paths(root)
    cfg = load_config(paths.config)
    n = count if count is not None else int(cfg["candidate_count"])

    generator = GenomeGenerator(
        seed=int(cfg["seed"]),
        timeframe=str(cfg["timeframe"]),
        can_short=bool(cfg["can_short"]),
    )
    genomes = generator.generate_many(n)

    generated_files: list[str] = []
    for genome in genomes:
        generated_files.append(str(write_strategy(genome, paths.generated).relative_to(root)))

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "seed": cfg["seed"],
        "count": n,
        "train_timerange": cfg["train_timerange"],
        "validation_timerange": cfg["validation_timerange"],
        "final_test_timerange": cfg["final_test_timerange"],
        "strategies": [g.to_dict() for g in genomes],
        "files": generated_files,
    }

    paths.manifests.mkdir(parents=True, exist_ok=True)
    manifest_path = paths.manifests / "latest_generation.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    first = genomes[0]
    commands = {
        "backtest_train": backtest_command(
            first.class_name, paths.freqtrade_config, paths.generated, cfg["train_timerange"]
        ),
        "hyperopt_train": hyperopt_command(
            first.class_name, paths.freqtrade_config, paths.generated, cfg["train_timerange"]
        ),
        "backtest_validation": backtest_command(
            first.class_name, paths.freqtrade_config, paths.generated, cfg["validation_timerange"]
        ),
        "lookahead": lookahead_command(
            first.class_name, paths.freqtrade_config, paths.generated, cfg["validation_timerange"]
        ),
        "recursive": recursive_command(
            first.class_name, paths.freqtrade_config, paths.generated, cfg["validation_timerange"]
        ),
    }

    return {
        "manifest": str(manifest_path),
        "generated": generated_files,
        "example_commands": commands,
    }
