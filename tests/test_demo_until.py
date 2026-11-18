"""demo.py: `until` stops the simulated day there, and the CLI's demo for today never writes the future
(SPEC "demo.py", added 2026-11-18: the README quickstart ran `demo` then `queue` and showed a negative elapsed time)."""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
import time

from robot_activity.demo import simulate_fleet
from robot_activity.queue import Queue
from robot_activity.store import Store

DAY = "2026-11-18"
DAY_START = datetime.datetime(2026, 11, 18, tzinfo=datetime.UTC).timestamp()
SIX_HOURS_IN = DAY_START + 6 * 3600


def test_nothing_is_written_after_until(tmp_path):
    simulate_fleet(tmp_path, day=DAY, seed=0, tz="UTC", until=SIX_HOURS_IN)
    store = Store(tmp_path, tz="UTC")
    latest_sample_t = max(sample.t for robot in store.robots() for sample in store.rows(robot, DAY))
    assert DAY_START + 5 * 3600 < latest_sample_t <= SIX_HOURS_IN
    queue = Queue(tmp_path)
    assert all(event["t"] <= SIX_HOURS_IN for event in queue.events())
    for item in queue.items():
        assert item.submitted_t <= SIX_HOURS_IN
        assert item.started_t is None or item.started_t <= SIX_HOURS_IN
        assert item.finished_t is None or item.finished_t <= SIX_HOURS_IN
    actions = (tmp_path / "agent" / "actions.jsonl").read_text().splitlines()
    assert actions and all(json.loads(line)["t"] <= SIX_HOURS_IN for line in actions)


def test_until_is_deterministic(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    for root in (first, second):
        simulate_fleet(root, day=DAY, seed=3, tz="UTC", until=SIX_HOURS_IN)
    def tree(root):
        return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}
    assert tree(first) == tree(second)


def test_cli_demo_for_today_never_writes_the_future(tmp_path):
    before = time.time()
    subprocess.run([sys.executable, "-m", "robot_activity.cli", "demo", "--root", str(tmp_path), "--tz", "UTC"],
                   check=True, capture_output=True, text=True)
    after = time.time()
    store = Store(tmp_path, tz="UTC")
    written = [sample.t for robot in store.robots() for day in store.days(robot) for sample in store.rows(robot, day)]
    assert written and max(written) <= after
    assert all(event["t"] <= after for event in Queue(tmp_path).events())
    for item in Queue(tmp_path).items(status="running"):
        assert item.started_t <= before + 60  # started before the demo ran: elapsed is never negative
