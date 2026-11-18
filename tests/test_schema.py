"""schema.py: Sample / QueueSnapshot, the row format a human can read (SPEC "schema.py")."""

from __future__ import annotations

import dataclasses
import json

import pytest

from robot_activity.schema import SCHEMA, QueueSnapshot, Sample

FULL = Sample(
    t=1794960010.5, robot="arm-1", sample_s=10.0, claimed_by="sim-to-real", item="job-17", moving=False, health="ok",
    reason="BUS DOWN (rig fault)",
    queue=QueueSnapshot(depth=3, runnable=1, running="job-17",
                        top_blocker={"item": "job-20", "owner": "pnp-agent", "kind": "code", "why": "IK refuses the bin pose"}),
    pose={"J1": 0.1, "J2": -1.25},
)


def test_schema_name():
    assert SCHEMA == "robot-activity/1"


def test_to_dict_names_the_schema_and_the_row():
    row = FULL.to_dict()
    assert row["schema"] == "robot-activity/1"
    assert row["t"] == 1794960010.5
    assert row["robot"] == "arm-1"
    assert row["claimed_by"] == "sim-to-real"
    assert row["moving"] is False


def test_round_trip_full_sample():
    assert Sample.from_dict(FULL.to_dict()) == FULL


def test_round_trip_through_json_text():
    text = json.dumps(FULL.to_dict())
    assert Sample.from_dict(json.loads(text)) == FULL


def test_round_trip_minimal_sample():
    minimal = Sample(t=5.0, robot="pendulum-1")
    assert Sample.from_dict(json.loads(json.dumps(minimal.to_dict()))) == minimal


@pytest.mark.parametrize("moving", [True, False, None])
def test_moving_keeps_its_three_values(moving):
    row = Sample(t=1.0, robot="arm-1", moving=moving)
    assert Sample.from_dict(row.to_dict()).moving is moving


def test_defaults():
    row = Sample(t=1.0, robot="arm-1")
    assert row.sample_s == 10.0
    assert row.claimed_by is None
    assert row.item is None
    assert row.moving is None
    assert row.health == "ok"
    assert row.reason is None
    assert row.queue is None
    assert row.pose is None


def test_none_fields_are_null_or_omitted():
    row = Sample(t=1.0, robot="arm-1").to_dict()
    for key in ("claimed_by", "item", "moving", "reason", "queue", "pose"):
        assert row.get(key) is None


def test_from_dict_ignores_unknown_keys():
    row = FULL.to_dict()
    row["written_by"] = "a newer sampler"
    row["extra"] = {"nested": [1, 2, 3]}
    assert Sample.from_dict(row) == FULL


def test_queue_snapshot_round_trips_inside_the_sample():
    back = Sample.from_dict(json.loads(json.dumps(FULL.to_dict())))
    assert isinstance(back.queue, QueueSnapshot)
    assert back.queue.top_blocker == {"item": "job-20", "owner": "pnp-agent", "kind": "code", "why": "IK refuses the bin pose"}


def test_queue_snapshot_defaults():
    snapshot = QueueSnapshot(depth=2, runnable=0)
    assert snapshot.running is None
    assert snapshot.top_blocker is None


def test_sample_is_frozen_and_keyword_only():
    with pytest.raises(dataclasses.FrozenInstanceError):
        FULL.moving = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        Sample(1.0, "arm-1")  # type: ignore[misc]
    with pytest.raises(TypeError):
        QueueSnapshot(1, 0)  # type: ignore[misc]
