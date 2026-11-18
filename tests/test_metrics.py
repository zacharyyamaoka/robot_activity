"""metrics.py: hours on the three splits, reason and owner tables, streaks, hand-off gaps, minutes, fleet
(SPEC "metrics.py"). Rows are hand-built with sample_s chosen so every expected number is exact."""

from __future__ import annotations

import dataclasses
import random

import pytest

from helpers import NOON, policy, sample
from robot_activity.metrics import (FleetSummary, Hours, OwnerHours, ReasonHours, Streak, fleet, handoff_gaps, hours,
                                    minutes, owners, streaks, why_dead, why_still)
from robot_activity.queue import Queue
from robot_activity.schema import QueueSnapshot

EXACT = 1e-9
EMPTY = QueueSnapshot(depth=0, runnable=0)
WAITING = QueueSnapshot(depth=2, runnable=2)

# claimed_by, item, moving, health, reason, queue, sample_s     -> side of each split, reason
DAY_SPEC = [
    ("alice", "j1", True, "ok", None, None, 360),                       # activity, moving
    ("alice", "j1", False, "ok", None, None, 720),                      # activity, still: "in activity (still)"
    ("alice", "j1", None, "ok", None, None, 180),                       # activity, unknown
    ("alice", None, False, "ok", None, EMPTY, 90),                      # dead, still: "queue empty"
    ("alice", None, False, "ok", None, WAITING, 540),                   # dead, still: "queue waiting (2 runnable, ...)"
    ("bob", "j2", False, "fault", None, None, 1080),                    # dead, still: "fault"
    ("bob", "j2", False, "fault", "BUS DOWN (rig fault)", None, 1440),  # dead, still: explicit reason
    (None, None, True, "ok", None, None, 36),                           # dead, moving: "unclaimed"
    (None, None, False, "ok", None, None, 270),                         # dead, still: "unclaimed"
    (None, None, None, "ok", None, None, 450),                          # dead, unknown: "unclaimed"
    ("bob", None, False, "ok", None, None, 126),                        # dead, still: "claimed, nothing dispatched"
    ("alice", "j1", False, "ok", None, None, 54),                       # activity, still: "in activity (still)"
]


def day_rows():
    return [sample(1000.0 + 100.0 * index, claimed_by=claimed_by, item=item, moving=moving, health=health, reason=reason,
                   queue=queue, sample_s=sample_s)
            for index, (claimed_by, item, moving, health, reason, queue, sample_s) in enumerate(DAY_SPEC)]


def h(seconds: float) -> float:
    return seconds / 3600.0


# --- hours ----------------------------------------------------------------------------------------------------------

def test_hours_exact():
    result = hours(day_rows())
    assert isinstance(result, Hours)
    assert result.tracked_h == pytest.approx(h(5346), abs=EXACT)
    assert result.claimed_h == pytest.approx(h(4590), abs=EXACT)
    assert result.unclaimed_h == pytest.approx(h(756), abs=EXACT)
    assert result.activity_h == pytest.approx(h(1314), abs=EXACT)
    assert result.dead_h == pytest.approx(h(4032), abs=EXACT)
    assert result.moving_h == pytest.approx(h(396), abs=EXACT)
    assert result.still_h == pytest.approx(h(4320), abs=EXACT)
    assert result.unknown_h == pytest.approx(h(630), abs=EXACT)
    assert result.utilization == pytest.approx(1314 / 5346, abs=EXACT)
    assert result.moving_share == pytest.approx(396 / 5346, abs=EXACT)


def test_hours_accepts_any_iterable():
    assert hours(iter(day_rows())) == hours(day_rows())


def test_hours_of_nothing():
    result = hours([])
    assert result.tracked_h == 0.0
    assert result.utilization == 0.0
    # SPEC states the 0.0 only for utilization; moving_share is the same ratio and is held to the same rule
    assert result.moving_share == 0.0


def test_unknown_is_never_still():
    result = hours([sample(10.0, claimed_by="alice", moving=None, sample_s=600)])
    assert result.unknown_h == pytest.approx(h(600), abs=EXACT)
    assert result.still_h == 0.0


def test_three_invariants_on_random_rows():
    generator = random.Random(20261118)
    rows = []
    for index in range(800):
        rows.append(sample(
            1000.0 + 10.0 * index,
            claimed_by=generator.choice([None, "alice", "bob", "carol"]),
            item=generator.choice([None, "j1", "j2"]),
            moving=generator.choice([True, False, None]),
            health=generator.choice(["ok", "ok", "ok", "fault", "disconnected"]),
            reason=generator.choice([None, None, None, "BUS DOWN (rig fault)"]),
            queue=generator.choice([None, EMPTY, WAITING]),
            sample_s=generator.uniform(0.5, 30.0)))
    result = hours(rows)
    assert result.tracked_h == pytest.approx(sum(row.sample_s for row in rows) / 3600.0, abs=EXACT)
    assert result.claimed_h + result.unclaimed_h == pytest.approx(result.tracked_h, abs=EXACT)
    assert result.activity_h + result.dead_h == pytest.approx(result.tracked_h, abs=EXACT)
    assert result.moving_h + result.still_h + result.unknown_h == pytest.approx(result.tracked_h, abs=EXACT)
    assert result.utilization == pytest.approx(result.activity_h / result.tracked_h, abs=EXACT)
    assert sum(entry.hours for entry in why_still(rows)) == pytest.approx(result.still_h, abs=EXACT)
    assert sum(entry.hours for entry in why_dead(rows)) == pytest.approx(result.dead_h, abs=EXACT)
    assert sum(entry.share for entry in why_still(rows)) == pytest.approx(1.0, abs=EXACT)
    assert sum(entry.hours for entry in owners(rows)) == pytest.approx(result.tracked_h, abs=EXACT)


# --- reason tables --------------------------------------------------------------------------------------------------

def assert_table(table, expected_seconds: list[tuple[str, float]]):
    total = sum(seconds for _, seconds in expected_seconds)
    assert [entry.reason for entry in table] == [reason for reason, _ in expected_seconds]
    for entry, (_, seconds) in zip(table, expected_seconds):
        assert isinstance(entry, ReasonHours)
        assert entry.hours == pytest.approx(h(seconds), abs=EXACT)
        assert entry.share == pytest.approx(seconds / total, abs=EXACT)


def test_why_still():
    assert_table(why_still(day_rows()), [
        ("BUS DOWN (rig fault)", 1440),
        ("fault", 1080),
        ("in activity (still)", 774),
        ("queue waiting (2 runnable, not started)", 540),
        ("unclaimed", 270),
        ("claimed, nothing dispatched", 126),
        ("queue empty", 90),
    ])


def test_why_dead():
    assert_table(why_dead(day_rows()), [
        ("BUS DOWN (rig fault)", 1440),
        ("fault", 1080),
        ("unclaimed", 756),
        ("queue waiting (2 runnable, not started)", 540),
        ("claimed, nothing dispatched", 126),
        ("queue empty", 90),
    ])


def test_reason_tables_of_nothing():
    assert why_still([]) == []
    assert why_dead([]) == []
    assert owners([]) == []


# --- owners ---------------------------------------------------------------------------------------------------------

def test_owners():
    table = owners(day_rows())
    expected = [  # owner, hours, moving, still, unknown (seconds)
        ("bob", 2646, 0, 2646, 0),
        ("alice", 1944, 360, 1404, 180),
        ("unclaimed", 756, 36, 270, 450),
    ]
    assert [entry.owner for entry in table] == [owner for owner, *_ in expected]
    for entry, (_, total, moving, still, unknown) in zip(table, expected):
        assert isinstance(entry, OwnerHours)
        assert entry.hours == pytest.approx(h(total), abs=EXACT)
        assert entry.moving_h == pytest.approx(h(moving), abs=EXACT)
        assert entry.still_h == pytest.approx(h(still), abs=EXACT)
        assert entry.unknown_h == pytest.approx(h(unknown), abs=EXACT)


# --- streaks --------------------------------------------------------------------------------------------------------

def streak_rows():
    queue_empty = dict(claimed_by="alice", moving=False, queue=EMPTY)
    unclaimed = dict(claimed_by=None, moving=False)
    return [
        sample(100.0, **queue_empty),
        sample(110.0, **queue_empty),
        sample(120.0, **unclaimed),
        sample(130.0, claimed_by="alice", item="j1", moving=True),   # activity breaks the dead run
        sample(140.0, **unclaimed),
        sample(155.0, **unclaimed),                                    # 15 s after: still consecutive (<= 1.5 x 10)
        sample(171.0, **unclaimed),                                    # 16 s after: a new streak
        sample(181.0, **unclaimed),
    ]


def test_dead_streaks_and_the_gap_rule():
    result = streaks(streak_rows(), "dead")
    assert all(isinstance(streak, Streak) and streak.kind == "dead" for streak in result)
    assert [(streak.start_t, streak.end_t) for streak in result] == [(90.0, 120.0), (130.0, 155.0), (161.0, 181.0)]
    first, second, third = result
    assert first.seconds == pytest.approx(30.0, abs=EXACT)
    assert first.reasons == pytest.approx({"queue empty": 20.0, "unclaimed": 10.0}, abs=EXACT)
    # SPEC ambiguity: for unevenly spaced rows "seconds" could be end_t - start_t (25) or the summed sample_s (20);
    # the second streak's seconds is therefore not pinned. Evenly spaced streaks agree on both readings.
    assert second.reasons == pytest.approx({"unclaimed": 20.0}, abs=EXACT)
    assert third.seconds == pytest.approx(20.0, abs=EXACT)
    assert third.reasons == pytest.approx({"unclaimed": 20.0}, abs=EXACT)


def test_activity_streaks():
    result = streaks(streak_rows(), "activity")
    assert [(streak.kind, streak.start_t, streak.end_t, streak.seconds) for streak in result] == [("activity", 120.0, 130.0, 10.0)]


def test_moving_streaks_break_on_unknown():
    rows = [sample(10.0, moving=True), sample(20.0, moving=True), sample(30.0, moving=None), sample(40.0, moving=True)]
    result = streaks(rows, "moving")
    assert [(streak.start_t, streak.end_t, streak.seconds) for streak in result] == [(0.0, 20.0, 20.0), (30.0, 40.0, 10.0)]


def test_streaks_of_nothing():
    assert streaks([], "dead") == []


# --- hand-off gaps --------------------------------------------------------------------------------------------------

def test_handoff_gaps_from_events():
    events = [
        {"t": 100.0, "kind": "start", "id": "a", "robot": "arm-1", "by": "x"},
        {"t": 150.0, "kind": "submit", "id": "c", "robot": "any", "by": "y"},
        {"t": 400.0, "kind": "finish", "id": "a", "robot": "arm-1", "by": None},
        {"t": 420.0, "kind": "start", "id": "c", "robot": "arm-2", "by": "y"},
        {"t": 460.0, "kind": "start", "id": "b", "robot": "arm-1", "by": "x"},
        {"t": 900.0, "kind": "finish", "id": "b", "robot": "arm-1", "by": None},
        {"t": 950.0, "kind": "finish", "id": "c", "robot": "arm-2", "by": None},
    ]
    assert handoff_gaps(events, "arm-1") == [
        {"after": "a", "next": "b", "ended_t": 400.0, "started_t": 460.0, "gap_s": 60.0}]
    assert handoff_gaps(events, "arm-2") == []
    assert handoff_gaps([], "arm-1") == []


def test_handoff_gaps_from_a_real_ledger(tmp_path):
    queue = Queue(tmp_path)
    first = queue.submit(title="pick vial", owner="pnp", est_min=5.0, robot="arm-1", policy=policy(), now=NOON)
    second = queue.submit(title="pick cap", owner="pnp", est_min=5.0, robot="arm-1", policy=policy(), now=NOON)
    queue.start(first.id, robot="arm-1", by="pnp", now=NOON + 1000)
    queue.finish(first.id, now=NOON + 1300)
    queue.start(second.id, robot="arm-1", by="pnp", now=NOON + 1375)
    queue.finish(second.id, now=NOON + 1500)
    assert handoff_gaps(queue.events(), "arm-1") == [
        {"after": first.id, "next": second.id, "ended_t": NOON + 1300, "started_t": NOON + 1375, "gap_s": 75.0}]


# --- minutes --------------------------------------------------------------------------------------------------------

def test_minutes():
    rows = [
        sample(60.0, claimed_by="alice", item="j1", moving=True, sample_s=15),
        sample(75.0, claimed_by="alice", item="j1", moving=False, sample_s=15),
        sample(90.0, claimed_by="bob", moving=None, sample_s=15),
        sample(119.0, claimed_by=None, moving=False, queue=QueueSnapshot(depth=3, runnable=1), sample_s=15),
        sample(120.0, claimed_by=None, moving=True, sample_s=15),
        sample(250.0, claimed_by="alice", item="j1", moving=None, queue=QueueSnapshot(depth=5, runnable=2), sample_s=15),
        sample(265.0, claimed_by="alice", item="j1", moving=None, queue=QueueSnapshot(depth=4, runnable=0), sample_s=15),
    ]
    result = minutes(rows)
    assert [minute["minute_t"] for minute in result] == [60, 120, 240]  # wall-clock minute of t; empty minutes absent
    keys = ("sampled_s", "claimed_frac", "activity_frac", "moving_frac", "unknown_frac", "queue_depth", "runnable")
    expected = [
        (60.0, 0.75, 0.5, 0.25, 0.25, 3, 1),
        (15.0, 0.0, 0.0, 1.0, 0.0, None, None),
        (30.0, 1.0, 1.0, 0.0, 1.0, 4, 0),   # queue fields from the minute's LAST row
    ]
    for minute, values in zip(result, expected):
        for key, value in zip(keys, values):
            if value is None:
                assert minute[key] is None, key
            else:
                assert minute[key] == pytest.approx(value, abs=EXACT), key


def test_minutes_of_nothing():
    assert minutes([]) == []


# --- fleet ----------------------------------------------------------------------------------------------------------

def test_fleet():
    now = 10_000.0
    rows_by_robot = {
        "arm-1": [sample(now - 20, robot="arm-1", claimed_by="a", item="j", moving=False, sample_s=600),
                  sample(now, robot="arm-1", claimed_by="a", item="j", moving=True, sample_s=600)],
        "arm-2": [sample(now - 10, robot="arm-2", claimed_by="b", item="k", moving=False, sample_s=1200)],
        "arm-3": [sample(now - 100, robot="arm-3", claimed_by="c", item="m", moving=True, sample_s=1800)],  # stale
        "pendulum-1": [sample(now - 60, robot="pendulum-1", moving=True, sample_s=900)],  # fresh at exactly fresh_s, dead
    }
    result = fleet(rows_by_robot, now)
    assert isinstance(result, FleetSummary)
    assert (result.robots, result.robots_active_now, result.robots_moving_now) == (4, 2, 2)
    assert result.tracked_h == pytest.approx(h(600 + 600 + 1200 + 1800 + 900), abs=EXACT)
    assert result.activity_h == pytest.approx(h(600 + 600 + 1200 + 1800), abs=EXACT)
    assert result.moving_h == pytest.approx(h(600 + 1800 + 900), abs=EXACT)
    assert result.utilization == pytest.approx(4200 / 5100, abs=EXACT)
    tighter = fleet(rows_by_robot, now, fresh_s=15.0)
    assert (tighter.robots_active_now, tighter.robots_moving_now) == (2, 1)


def test_dataclasses_are_frozen():
    for value in (hours([]), ReasonHours("r", 1.0, 1.0), OwnerHours("o", 1.0, 0.0, 1.0, 0.0)):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, dataclasses.fields(value)[0].name, None)
