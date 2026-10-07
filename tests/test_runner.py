"""Unit tests for strategy_miner.runner (no docker, no network)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

import pytest

from strategy_miner.runner import (
    CONTAINER_USERDATA,
    DEFAULT_IMAGE,
    DockerRunner,
    RunnerError,
    RunResult,
)


def _posix(path: Path) -> str:
    return str(path).replace("\\", "/")


class ScriptRunner(DockerRunner):
    """DockerRunner whose build_command runs a local python one-liner instead."""

    def __init__(self, script: str) -> None:
        super().__init__(Path.cwd())
        self._script = script

    def build_command(self, args: Sequence[str]) -> list[str]:
        return [sys.executable, "-c", self._script]


class TestBuildCommand:
    def test_exact_argv(self, tmp_path: Path) -> None:
        user_data = tmp_path / "freqtrade" / "user_data"
        runner = DockerRunner(user_data)
        cmd = runner.build_command(
            ["backtesting", "--config", f"{CONTAINER_USERDATA}/config.miner.json"]
        )
        assert cmd == [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{_posix(user_data)}:{CONTAINER_USERDATA}",
            DEFAULT_IMAGE,
            "backtesting",
            "--config",
            f"{CONTAINER_USERDATA}/config.miner.json",
        ]

    def test_custom_image_and_binary(self, tmp_path: Path) -> None:
        user_data = tmp_path / "user_data"
        runner = DockerRunner(user_data, image="ft:dev", docker_bin="podman")
        cmd = runner.build_command(["--version"])
        assert cmd == [
            "podman",
            "run",
            "--rm",
            "-v",
            f"{_posix(user_data)}:{CONTAINER_USERDATA}",
            "ft:dev",
            "--version",
        ]


class TestMapPath:
    def test_inside(self, tmp_path: Path) -> None:
        user_data = tmp_path / "user_data"
        runner = DockerRunner(user_data)
        assert (
            runner.map_path(user_data / "config.miner.json")
            == f"{CONTAINER_USERDATA}/config.miner.json"
        )
        nested = user_data / "strategies" / "generated" / "MyStrat.py"
        assert (
            runner.map_path(nested)
            == f"{CONTAINER_USERDATA}/strategies/generated/MyStrat.py"
        )

    def test_outside_raises(self, tmp_path: Path) -> None:
        runner = DockerRunner(tmp_path / "user_data")
        with pytest.raises(ValueError):
            runner.map_path(tmp_path / "elsewhere" / "config.json")
        # path-component prefix must not count as "under root"
        with pytest.raises(ValueError):
            runner.map_path(tmp_path / "user_data_secret" / "x.json")


class TestRun:
    def test_exit_code_stdout_argv_duration(self) -> None:
        runner = ScriptRunner("print('hi'); import sys; sys.exit(3)")
        result = runner.run(["backtesting"])
        assert isinstance(result, RunResult)
        assert result.exit_code == 3
        assert result.stdout == "hi\n"
        assert result.argv == (sys.executable, "-c", "print('hi'); import sys; sys.exit(3)")
        assert isinstance(result.duration_s, float)
        assert result.duration_s >= 0.0
        assert result.log_path is None

    def test_combines_stderr(self) -> None:
        runner = ScriptRunner("import sys; sys.stderr.write('boom\\n')")
        result = runner.run([])
        assert result.exit_code == 0
        assert "boom" in result.stdout

    def test_strips_ansi_escapes(self) -> None:
        runner = ScriptRunner("import sys; sys.stdout.write('\\x1b[31mred\\x1b[0m\\n')")
        result = runner.run([])
        assert result.stdout == "red\n"

    def test_timeout(self) -> None:
        runner = ScriptRunner("import time; time.sleep(30)")
        result = runner.run([], timeout_s=1)
        assert result.exit_code == 124
        assert result.stdout.startswith("TIMEOUT after 1s")

    def test_timeout_includes_partial_output(self) -> None:
        runner = ScriptRunner(
            "import sys, time; sys.stdout.write('partial\\n'); sys.stdout.flush(); "
            "time.sleep(30)"
        )
        result = runner.run([], timeout_s=1)
        assert result.exit_code == 124
        assert result.stdout.startswith("TIMEOUT after 1s")
        assert "partial" in result.stdout

    def test_missing_docker_binary_raises(self, tmp_path: Path) -> None:
        runner = DockerRunner(tmp_path / "user_data", docker_bin="no-such-docker-binary-xyz")
        with pytest.raises(RunnerError):
            runner.run(["backtesting"])

    def test_writes_log(self, tmp_path: Path) -> None:
        runner = ScriptRunner("print('hello')")
        log_dir = tmp_path / "logs"
        result = runner.run([], log_dir=log_dir, log_name="run.log")
        assert result.log_path == log_dir / "run.log"
        assert result.log_path is not None
        assert result.log_path.exists()
        assert "hello" in result.log_path.read_text(encoding="utf-8")

    def test_timeout_writes_log(self, tmp_path: Path) -> None:
        runner = ScriptRunner("import time; time.sleep(30)")
        log_dir = tmp_path / "logs"
        result = runner.run([], timeout_s=1, log_dir=log_dir, log_name="timeout.log")
        assert result.exit_code == 124
        assert result.log_path is not None
        assert result.log_path.exists()
        assert result.log_path.read_text(encoding="utf-8").startswith("TIMEOUT after 1s")
