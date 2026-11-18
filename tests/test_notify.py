"""notify.py: Outbox (JSON lines) and CommandNotifier (argv with the action's JSON on stdin) (SPEC "notify.py")."""

from __future__ import annotations

import json
import sys
import time

from robot_activity.agent import Action
from robot_activity.notify import CommandNotifier, Outbox

ACTION = Action(kind="start_next", robot="arm-1", to=("rig-runner", "dispatcher"), message="start pick vial (4 min)",
                item="job-17", policy="https://github.com/example/pick-policies@0123456789ab:policies/pick.py",
                key="start_next:arm-1:job-17")


def test_outbox_appends_one_json_line_per_action(tmp_path):
    path = tmp_path / "outbox.jsonl"
    outbox = Outbox(path)
    assert outbox.deliver(ACTION) is True
    assert outbox.deliver(ACTION) is True
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    record = json.loads(lines[0])
    assert (record["kind"], record["robot"], record["to"], record["message"], record["item"], record["policy"]) == (
        "start_next", "arm-1", ["rig-runner", "dispatcher"], "start pick vial (4 min)", "job-17", ACTION.policy)


COPY_STDIN = "import json, pathlib, sys; pathlib.Path(sys.argv[1]).write_text(json.dumps(json.load(sys.stdin)))"


def test_command_notifier_passes_the_action_json_on_stdin(tmp_path):
    out = tmp_path / "received.json"
    notifier = CommandNotifier([sys.executable, "-c", COPY_STDIN, str(out)])
    assert notifier.deliver(ACTION) is True
    received = json.loads(out.read_text())
    assert (received["kind"], received["robot"], received["to"], received["message"], received["item"]) == (
        "start_next", "arm-1", ["rig-runner", "dispatcher"], "start pick vial (4 min)", "job-17")


def test_command_notifier_failure_is_false():
    notifier = CommandNotifier([sys.executable, "-c", "import sys; sys.stdin.read(); sys.exit(3)"])
    assert notifier.deliver(ACTION) is False


def test_command_notifier_timeout_is_false_not_a_hang():
    notifier = CommandNotifier([sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=0.5)
    started = time.monotonic()
    assert notifier.deliver(ACTION) is False
    assert time.monotonic() - started < 15.0
