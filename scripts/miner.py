"""Shim entry point: ``python scripts/miner.py <cmd>`` (works from any cwd)."""

from __future__ import annotations

import sys
from pathlib import Path


def _repo_root() -> Path:
    """Repo root is the parent of this script's directory."""
    return Path(__file__).resolve().parents[1]


def main() -> int:
    """Insert ``<repo>/src`` into ``sys.path`` and run the CLI."""
    src = _repo_root() / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from strategy_miner.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
