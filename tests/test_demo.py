"""demo.py: a deterministic simulated fleet day that exercises every rule (SPEC "demo.py")."""

from __future__ import annotations

import bisect
import datetime
from pathlib import Path

import pytest

from robot_activity.classify import classify
from robot_activity.demo import simulate_fleet
from robot_activity.queue import BLOCKER_KINDS, Queue
from robot_activity.stalemate import assess
from robot_activity.store import Store

DAY = "2026-11-18"
DAY_START = datetime.datetime(2026, 11, 18, tzinfo=datetime.timezone.utc).timestamp()
ROBOTS = ("arm-1", "arm-2", "pendulum-1")


def tree(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    roots = {name: tmp_path_factory.mktemp(name) for name in ("seed0_a", "seed0_b", "seed1")}
    results = {
        "seed0_a": simulate_fleet(roots["seed0_a"], seed=0),
        "seed0_b": simulate_fleet(roots["seed0_b"], seed=0),
        "seed1": simulate_fleet(roots["seed1"], seed=1),
    }
    return roots, results


def test_same_seed_is_byte_identical(runs):
    roots, _ = runs
    first, second = tree(roots["seed0_a"]), tree(roots["seed0_b"])
    assert first, "the demo wrote files"
    assert first.keys() == second.keys()
    for name in first:
        assert first[name] == second[name], name


def test_different_seed_differs(runs):
    roots, _ = runs
    assert tree(roots["seed0_a"]) != tree(roots["seed1"])


def test_returned_counts(runs):
    roots, results = runs
    store, queue = Store(roots["seed0_a"], tz="UTC"), Queue(roots["seed0_a"])
    assert results["seed0_a"]["rows"] == sum(len(store.rows(robot, DAY)) for robot in store.robots())
    assert results["seed0_a"]["items"] == len(queue.items())


@pytest.fixture(scope="module")
def day(runs):
    roots, _ = runs
    store, queue = Store(roots["seed0_a"], tz="UTC"), Queue(roots["seed0_a"])
    return store, queue, {robot: store.rows(robot, DAY) for robot in store.robots()}


def test_every_robot_sampled_over_the_day(day):
    store, _, rows_by_robot = day
    assert set(ROBOTS) <= set(store.robots())
    assert len(rows_by_robot) >= 3
    for robot in ROBOTS:
        assert store.days(robot) == [DAY]
        rows = rows_by_robot[robot]
        assert rows[0].t < DAY_START + 3600 and rows[-1].t > DAY_START + 23 * 3600, robot
        assert all(row.sample_s == 10.0 for row in rows), robot


def test_a_fault_episode_with_an_explicit_reason(day):
    _, _, rows_by_robot = day
    assert any(row.health != "ok" or row.reason is not None for rows in rows_by_robot.values() for row in rows)


def test_queue_waiting_stretches(day):
    _, _, rows_by_robot = day
    assert any((classify(row).reason or "").startswith("queue waiting (")
               for rows in rows_by_robot.values() for row in rows)


def test_items_have_owners_policies_fillers_and_blockers(day):
    _, queue, _ = day
    items = queue.items()
    assert all(item.owner for item in items)
    assert any(item.policy is not None for item in items)
    assert any(item.source == "filler" for item in items)
    blocker_kinds = {blocker.kind for item in items for blocker in item.blockers}
    for event in queue.events():
        if event["kind"] == "block":
            blocker_kinds.add(event["blocker"]["kind"])
    assert len(blocker_kinds & set(BLOCKER_KINDS)) >= 2


def test_at_least_one_looping_stalemate(day):
    _, queue, rows_by_robot = day
    items = queue.items()
    for robot, rows in rows_by_robot.items():
        times = [row.t for row in rows]
        for now in range(int(DAY_START) + 600, int(DAY_START) + 86400 + 1, 60):
            window = rows[bisect.bisect_left(times, now - 700):bisect.bisect_right(times, now)]
            if any(flag.kind == "looping" for flag in assess(window, items, float(now))):
                return
    pytest.fail("no looping stalemate anywhere in the simulated day")
