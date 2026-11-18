"""sampler.py: one Sample per step, motion folded from fed joints, queue snapshot and item fallback (SPEC "sampler.py")."""

from __future__ import annotations

from robot_msgs.sensor import JointState

from helpers import NOON, policy, start_running
from robot_activity.motion import MotionDetector
from robot_activity.queue import Queue
from robot_activity.sampler import Observation, Sampler
from robot_activity.store import Store


def probe_returning(**fields):
    return lambda: Observation(**fields)


def test_observation_defaults():
    observation = Observation()
    assert (observation.claimed_by, observation.item, observation.joints, observation.health, observation.reason) == (
        None, None, None, "ok", None)


def test_step_writes_one_sample_per_call(tmp_path):
    store = Store(tmp_path, tz="UTC")
    sampler = Sampler(store, "arm-1", probe_returning(claimed_by="alice"))
    first = sampler.step(NOON)
    second = sampler.step(NOON + 10)
    assert store.rows("arm-1", store.day_of(NOON)) == [first, second]
    assert (first.t, first.robot, first.sample_s, first.claimed_by) == (NOON, "arm-1", 10.0, "alice")
    assert first.moving is None  # no joints at all: unknown, never still
    assert first.queue is None   # no queue given


def test_motion_from_fed_joints(tmp_path):
    store = Store(tmp_path, tz="UTC")
    sampler = Sampler(store, "arm-1", probe_returning(claimed_by="alice"))
    sampler.feed(NOON - 8, {"J1": 0.0, "J2": 1.0})
    sampler.feed(NOON - 3, {"J1": 0.02, "J2": 1.0})
    moving = sampler.step(NOON)
    assert moving.moving is True
    assert moving.pose == {"J1": 0.02, "J2": 1.0}

    sampler.feed(NOON + 2, {"J1": 0.02, "J2": 1.0})
    sampler.feed(NOON + 6, {"J1": 0.02, "J2": 1.0})
    assert sampler.step(NOON + 10).moving is False

    assert sampler.step(NOON + 30).moving is None  # nothing fed in (NOON+20, NOON+30]


def test_probe_joints_are_folded_in(tmp_path):
    store = Store(tmp_path, tz="UTC")
    probe = probe_returning(claimed_by="alice", joints=JointState(name=("J1",), position=[0.05]))
    sampler = Sampler(store, "arm-1", probe)
    sampler.feed(NOON - 5, {"J1": 0.0})
    row = sampler.step(NOON)
    assert row.moving is True
    assert row.pose == {"J1": 0.05}


def test_a_given_motion_detector_is_used(tmp_path):
    store = Store(tmp_path, tz="UTC")
    sampler = Sampler(store, "arm-1", probe_returning(), motion=MotionDetector(threshold=1.0))
    sampler.feed(NOON - 8, {"J1": 0.0})
    sampler.feed(NOON - 3, {"J1": 0.5})
    assert sampler.step(NOON).moving is False


def test_queue_snapshot_and_item_fallback(tmp_path):
    store = Store(tmp_path, tz="UTC")
    queue = Queue(tmp_path)
    running = start_running(queue, started_t=NOON - 100)
    queue.submit(title="pick cap", owner="pnp-agent", est_min=4.0, robot="arm-1", policy=policy(), now=NOON - 50)
    row = Sampler(store, "arm-1", probe_returning(claimed_by="pnp-agent"), queue=queue).step(NOON)
    assert row.item == running.id  # the probe did not say; the queue's running item did
    assert row.queue == queue.snapshot("arm-1")
    assert (row.queue.depth, row.queue.runnable, row.queue.running) == (1, 1, running.id)


def test_probe_item_health_reason_win(tmp_path):
    store = Store(tmp_path, tz="UTC")
    queue = Queue(tmp_path)
    start_running(queue, started_t=NOON - 100)
    probe = probe_returning(claimed_by="pnp-agent", item="probe-job", health="fault", reason="BUS DOWN (rig fault)")
    row = Sampler(store, "arm-1", probe, queue=queue, sample_s=5.0).step(NOON)
    assert (row.item, row.health, row.reason, row.sample_s) == ("probe-job", "fault", "BUS DOWN (rig fault)", 5.0)
    assert store.latest("arm-1") == row
