"""wandb_log.payload / fleet_payload are pure: built without importing wandb (SPEC "wandb_log.py").

The payloads are computed in a fresh interpreter whose path starts with a stand-in `wandb` package, so an import of
wandb anywhere on that path would show up in sys.modules even when the real library is not installed.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from helpers import DAY, NOON, policy, series, write
from procs import run_python
from robot_activity.metrics import FleetSummary, Hours, hours, minutes, why_still
from robot_activity.queue import Queue
from robot_activity.store import Store


def build(root):
    store = Store(root, tz="UTC")
    queue = Queue(root)
    item = queue.submit(title="pick vial", owner="pnp-agent", est_min=4.0, robot="arm-1", policy=policy(), now=NOON - 4000)
    queue.start(item.id, robot="arm-1", by="pnp-agent", now=NOON - 3600)
    queue.finish(item.id, now=NOON - 3000)
    write(store, series(NOON - 3600, NOON - 3000, claimed_by="pnp-agent", item=item.id, moving=True))
    write(store, series(NOON - 2990, NOON - 1000, claimed_by="pnp-agent", moving=False))
    write(store, series(NOON - 990, NOON, moving=None))
    write(store, series(NOON - 1800, NOON, robot="pendulum-1", claimed_by="rack-agent", moving=True))
    return store, queue


SCRIPT = """
import json, sys
from robot_activity.store import Store
from robot_activity.queue import Queue
from robot_activity import wandb_log
store, queue = Store({root!r}, tz="UTC"), Queue({root!r})
robot_payload = wandb_log.payload(store, queue, "arm-1", {day!r})
fleet_payload = wandb_log.fleet_payload(store, {day!r})
print(json.dumps({{"payload": robot_payload, "fleet": fleet_payload, "wandb_imported": "wandb" in sys.modules}},
                 default=str))
"""


@pytest.fixture
def payloads(tmp_path, fake_heavy_modules):
    root = tmp_path / "root"
    store, _ = build(root)
    result = run_python(SCRIPT.format(root=str(root), day=DAY), first_on_path=fake_heavy_modules)
    assert result.returncode == 0, result.stderr
    return store, json.loads(result.stdout)


def test_payload_never_imports_wandb(payloads):
    _, output = payloads
    assert output["wandb_imported"] is False


def test_robot_payload_shape(payloads):
    store, output = payloads
    payload = output["payload"]
    rows = store.rows("arm-1", DAY)
    assert payload["run_id"] == f"arm-1-{DAY}"
    assert len(payload["steps"]) == len(minutes(rows))
    assert [step["minute_t"] for step in payload["steps"]] == [minute["minute_t"] for minute in minutes(rows)]
    expected_summary = dataclasses.asdict(hours(rows))
    assert set(payload["summary"]) == {field.name for field in dataclasses.fields(Hours)}
    for key, value in expected_summary.items():
        assert payload["summary"][key] == pytest.approx(value, abs=1e-9), key
    assert set(payload["tables"]) == {"why_still", "owners", "streaks", "handoff_gaps"}
    assert len(payload["tables"]["why_still"]) == len(why_still(rows))


def test_fleet_payload_shape(payloads):
    store, output = payloads
    fleet_payload = output["fleet"]
    assert fleet_payload["run_id"] == f"fleet-{DAY}"
    assert set(fleet_payload["summary"]) == {field.name for field in dataclasses.fields(FleetSummary)}
    assert fleet_payload["summary"]["robots"] == 2
    tracked = sum(row.sample_s for robot in ("arm-1", "pendulum-1") for row in store.rows(robot, DAY)) / 3600.0
    assert fleet_payload["summary"]["tracked_h"] == pytest.approx(tracked, abs=1e-9)
