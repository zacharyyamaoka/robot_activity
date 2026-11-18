"""Run Python and the CLI in fresh subprocesses (no robot_activity import here)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _env(first_on_path: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    if first_on_path is not None:
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join([str(first_on_path)] + ([existing] if existing else []))
    return env


def run_python(code: str, *, first_on_path: Path | None = None, timeout: float = 300.0) -> subprocess.CompletedProcess:
    """`python -c code` in a fresh interpreter, cwd = the repo."""
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=timeout,
                          env=_env(first_on_path), cwd=REPO)


def run_cli(*args: str, timeout: float = 600.0) -> subprocess.CompletedProcess:
    """`python -m robot_activity.cli ARGS` in a fresh interpreter."""
    return subprocess.run([sys.executable, "-m", "robot_activity.cli", *args], capture_output=True, text=True,
                          timeout=timeout, env=_env(None), cwd=REPO)
