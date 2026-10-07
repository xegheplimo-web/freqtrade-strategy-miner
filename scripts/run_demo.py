from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from strategy_miner.orchestrator import generate_batch


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate sample Freqtrade strategies from deterministic genomes.")
    parser.add_argument("--count", type=int, default=10, help="Number of candidates to generate")
    args = parser.parse_args()

    if args.count < 1:
        raise SystemExit("--count must be >= 1")

    result = generate_batch(ROOT, count=args.count)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
