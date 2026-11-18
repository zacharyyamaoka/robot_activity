"""Where the agent's actions go: a JSON-lines outbox, or any command that reads the action's JSON on stdin.

A command notifier is the bridge to whatever the team already uses: a Slack webhook script, a mail relay, or
`claude -p` for an agent that should act on the prompt. `deliver` returns True only when the command exited 0, and
never hangs past its timeout.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Protocol

from robot_activity.agent import Action, action_record


class Notifier(Protocol):
    def deliver(self, action: Action) -> bool: ...


class Outbox:
    """Append each action as one JSON line (with the wall-clock time it was delivered)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def deliver(self, action: Action) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(action_record(action, delivered_t=time.time())) + "\n")
        return True


class CommandNotifier:
    """Run `argv` with the action's JSON on stdin; delivered iff the command exits 0 within timeout_s."""

    def __init__(self, argv: list[str], timeout_s: float = 30.0):
        self.argv = list(argv)
        self.timeout_s = timeout_s

    def deliver(self, action: Action) -> bool:
        try:
            completed = subprocess.run(self.argv, input=json.dumps(action_record(action)), text=True,
                                       capture_output=True, timeout=self.timeout_s, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0
