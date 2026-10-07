"""Docker execution surface for running freqtrade CLI commands in a container.

FROZEN contract: .orchestrator/INTERFACES.md section 1 (2026-10-08).
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, Sequence

DEFAULT_IMAGE = "freqtradeorg/freqtrade:stable"
CONTAINER_USERDATA = "/freqtrade/user_data"

# CSI escape sequences, e.g. "\x1b[31m" (color) or "\x1b[0m" (reset).
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class RunnerError(RuntimeError):
    """Raised when a command cannot be executed at all (e.g. docker binary missing)."""


@dataclass(frozen=True)
class RunResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    duration_s: float
    log_path: Path | None = None


class Runner(Protocol):
    """Any object with these two methods is accepted as a runner."""

    def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: int = 3600,
        log_dir: Path | None = None,
        log_name: str | None = None,
    ) -> RunResult: ...

    def map_path(self, host_path: Path) -> str: ...


def _posix(path: Path) -> str:
    """Host path rendered with forward slashes (posix style)."""
    return str(path).replace("\\", "/")


def _decode(data: bytes | str | None) -> str:
    """Decode subprocess output as utf-8, never crashing on undecodable bytes."""
    if not data:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _normalize_newlines(text: str) -> str:
    """Normalize CRLF to LF so captured output is platform-independent."""
    return text.replace("\r\n", "\n")


class DockerRunner:
    """Runs freqtrade CLI args inside the freqtrade docker image."""

    def __init__(
        self,
        user_data_dir: Path,
        *,
        image: str = DEFAULT_IMAGE,
        docker_bin: str = "docker",
    ) -> None:
        self.user_data_dir = Path(user_data_dir)
        self.image = image
        self.docker_bin = docker_bin

    def map_path(self, host_path: Path) -> str:
        host = Path(host_path)
        try:
            rel = host.resolve().relative_to(self.user_data_dir.resolve())
        except ValueError:
            raise ValueError(
                f"path {host} is not under user_data_dir {self.user_data_dir}"
            ) from None
        if rel == Path("."):
            return CONTAINER_USERDATA
        return f"{CONTAINER_USERDATA}/{_posix(rel)}"

    def build_command(self, args: Sequence[str]) -> list[str]:
        return [
            self.docker_bin,
            "run",
            "--rm",
            "-v",
            f"{_posix(self.user_data_dir)}:{CONTAINER_USERDATA}",
            self.image,
            *args,
        ]

    def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: int = 3600,
        log_dir: Path | None = None,
        log_name: str | None = None,
    ) -> RunResult:
        cmd = self.build_command(args)
        start = time.monotonic()
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            partial = _normalize_newlines(_strip_ansi(_decode(exc.stdout) + _decode(exc.stderr)))
            result = RunResult(
                argv=tuple(cmd),
                exit_code=124,
                stdout=f"TIMEOUT after {timeout_s}s\n{partial}",
                duration_s=time.monotonic() - start,
            )
        except FileNotFoundError as exc:
            raise RunnerError(f"docker binary not found: {self.docker_bin!r}") from exc
        else:
            combined = _normalize_newlines(_strip_ansi(_decode(proc.stdout) + _decode(proc.stderr)))
            result = RunResult(
                argv=tuple(cmd),
                exit_code=proc.returncode,
                stdout=combined,
                duration_s=time.monotonic() - start,
            )
        if log_dir is not None and log_name is not None:
            log_path = Path(log_dir) / log_name
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(result.stdout, encoding="utf-8")
            result = replace(result, log_path=log_path)
        return result
