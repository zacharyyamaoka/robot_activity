"""stalemate.py: activity that loops in place, each rule with a positive and a near-miss (SPEC "stalemate.py").

Rows are 10 s apart, all on arm-1, claimed by the running item's owner.
"""

from __future__ import annotations

import pytest

from helpers import NOON, deg, motion_rows, policy, start_running
from robot_activity.queue import Queue
from robot_activity.stalemate import Flag, assess

POSE_A = {"J1": deg(0.2), "J2": deg(30.2)}
POSE_A_NEAR = {"J1": deg(0.6), "J2": deg(30.6)}   # same 2-degree cell as A
POSE_B = {"J1": deg(10.4), "J2": deg(30.2)}
POSE_C = {"J1": deg(20.4), "J2": deg(30.2)}
SWING = {"J1": deg(5.0), "J2": deg(40.0)}


@pytest.fixture
def queue(tmp_path) -> Queue:
    return Queue(tmp_path)


def kinds(flags) -> list[str]:
    return sorted(flag.kind for flag in flags)


def moving(count: int):
    return [(True, SWING)] * count


def still(count: int, pose=POSE_A):
    return [(False, pose)] * count


def unknown(count: int):
    return [(None, None)] * count


def ending_at(now: float, pattern, item) -> list:
    """Rows whose last one is at `now`."""
    return motion_rows(now - 10.0 * (len(pattern) - 1), pattern, claimed_by=item.owner, item=item.id)


# --- stuck ----------------------------------------------------------------------------------------------------------

def test_stuck_after_still_limit(queue):
    item = start_running(queue, started_t=NOON - 3000)
    rows = ending_at(NOON, moving(3) + still(31), item)
    flags = assess(rows, queue.items(), NOON)
    assert kinds(flags) == ["stuck"]
    flag = flags[0]
    assert isinstance(flag, Flag)
    assert (flag.robot, flag.item) == ("arm-1", item.id)
    assert flag.message
    first_still_t = rows[3].t
    assert first_still_t - 10.0 <= flag.since_t <= first_still_t


def test_stuck_boundary_is_inclusive(queue):
    item = start_running(queue, started_t=NOON - 3000)
    assert kinds(assess(ending_at(NOON, moving(3) + still(30), item), queue.items(), NOON)) == ["stuck"]  # 300 s


def test_not_stuck_just_under_the_limit(queue):
    item = start_running(queue, started_t=NOON - 3000)
    assert assess(ending_at(NOON, moving(3) + still(29), item), queue.items(), NOON) == []  # 290 s


def test_unknown_does_not_count_as_still(queue):
    item = start_running(queue, started_t=NOON - 3000)
    rows = ending_at(NOON, moving(3) + still(20) + unknown(15) + still(5), item)  # 250 s still over 400 s of rows
    assert assess(rows, queue.items(), NOON) == []


def test_unknown_resets_nothing(queue):
    item = start_running(queue, started_t=NOON - 3000)
    rows = ending_at(NOON, moving(3) + still(20) + unknown(5) + still(12), item)  # 320 s still
    assert kinds(assess(rows, queue.items(), NOON)) == ["stuck"]
    trailing_unknown = ending_at(NOON, moving(3) + still(31) + unknown(3), item)
    assert kinds(assess(trailing_unknown, queue.items(), NOON)) == ["stuck"]


def test_not_stuck_without_a_running_item(queue):
    finished = start_running(queue, started_t=NOON - 3000)
    queue.finish(finished.id, now=NOON - 2000)
    rows = ending_at(NOON, still(60), finished)
    assert assess(rows, queue.items(), NOON) == []


def test_still_limit_parameter(queue):
    item = start_running(queue, started_t=NOON - 3000)
    rows = ending_at(NOON, moving(3) + still(12), item)  # 120 s
    assert assess(rows, queue.items(), NOON) == []
    assert kinds(assess(rows, queue.items(), NOON, still_limit_s=120.0)) == ["stuck"]


# --- looping --------------------------------------------------------------------------------------------------------

def three_rests_two_poses():
    return moving(3) + still(3, POSE_A) + moving(3) + still(3, POSE_B) + moving(3) + still(3, POSE_A_NEAR) + moving(3)


def test_looping_three_rests_at_two_poses(queue):
    item = start_running(queue, started_t=NOON - 3000)
    flags = assess(ending_at(NOON, three_rests_two_poses(), item), queue.items(), NOON)
    assert kinds(flags) == ["looping"]
    # SPEC: Flag.item is the item, when there is one; a looping flag on a robot running an item names that item
    assert (flags[0].robot, flags[0].item) == ("arm-1", item.id)
    assert flags[0].message


def test_not_looping_with_two_rests(queue):
    item = start_running(queue, started_t=NOON - 3000)
    pattern = moving(3) + still(3, POSE_A) + moving(3) + still(3, POSE_B) + moving(3)
    assert assess(ending_at(NOON, pattern, item), queue.items(), NOON) == []


def test_not_looping_at_three_distinct_poses(queue):
    item = start_running(queue, started_t=NOON - 3000)
    pattern = moving(3) + still(3, POSE_A) + moving(3) + still(3, POSE_B) + moving(3) + still(3, POSE_C) + moving(3)
    assert assess(ending_at(NOON, pattern, item), queue.items(), NOON) == []


def test_not_looping_when_an_item_finished_in_the_window(queue):
    done = start_running(queue, started_t=NOON - 600, title="pick cap")
    queue.finish(done.id, now=NOON - 250)
    item = start_running(queue, started_t=NOON - 240)
    assert assess(ending_at(NOON, three_rests_two_poses(), item), queue.items(), NOON) == []


def test_rests_outside_the_window_do_not_count(queue):
    item = start_running(queue, started_t=NOON - 3000)
    pattern = moving(3) + still(3, POSE_A) + moving(50) + still(3, POSE_B) + moving(3) + still(3, POSE_A_NEAR) + moving(3)
    rows = ending_at(NOON, pattern, item)
    assert rows[3].t < NOON - 600  # the first rest is outside the default 600 s window
    assert assess(rows, queue.items(), NOON) == []
    assert kinds(assess(rows, queue.items(), NOON, loop_window_s=1000.0)) == ["looping"]


def test_pose_grid_parameter(queue):
    item = start_running(queue, started_t=NOON - 3000)
    pattern = moving(3) + still(3, POSE_A) + moving(3) + still(3, POSE_B) + moving(3) + still(3, POSE_C) + moving(3)
    rows = ending_at(NOON, pattern, item)
    assert kinds(assess(rows, queue.items(), NOON, pose_grid=deg(90.0))) == ["looping"]  # all three rests in one cell


# --- overrun --------------------------------------------------------------------------------------------------------

def test_overrun_past_factor_times_estimate_plus_slack(queue):
    item = start_running(queue, started_t=NOON - 301, est_min=2.0)  # limit 1.5 * 120 + 120 = 300 s
    flags = assess(ending_at(NOON, moving(20), item), queue.items(), NOON)
    assert kinds(flags) == ["overrun"]
    assert (flags[0].robot, flags[0].item) == ("arm-1", item.id)


def test_no_overrun_before_the_limit(queue):
    item = start_running(queue, started_t=NOON - 299, est_min=2.0)
    assert assess(ending_at(NOON, moving(20), item), queue.items(), NOON) == []


def test_overrun_parameters(queue):
    item = start_running(queue, started_t=NOON - 121, est_min=2.0)
    rows = ending_at(NOON, moving(10), item)
    assert assess(rows, queue.items(), NOON) == []
    flags = assess(rows, queue.items(), NOON, overrun_factor=1.0, overrun_slack_s=0.0)  # limit 120 s
    assert kinds(flags) == ["overrun"]


def test_finished_item_never_overruns(queue):
    item = start_running(queue, started_t=NOON - 5000, est_min=1.0)
    queue.finish(item.id, now=NOON - 10)
    assert assess(ending_at(NOON, moving(5), item), queue.items(), NOON) == []


def test_other_robot_item_does_not_flag_this_robot(queue):
    elsewhere = start_running(queue, started_t=NOON - 5000, est_min=1.0, robot="arm-2")
    rows = motion_rows(NOON - 400, still(41), claimed_by="pnp-agent")  # arm-1 still 410 s, nothing running on arm-1
    flags = assess(rows, queue.items(), NOON)
    assert all(flag.robot != "arm-1" for flag in flags)
    assert all(flag.item == elsewhere.id for flag in flags)


def test_queued_item_never_flags(queue):
    # an item that never started produces no flag, however old
    queue.submit(title="sweep", owner="sweep-agent", est_min=1.0, robot="arm-1", policy=policy("sweep"), now=NOON - 9000)
    item = start_running(queue, started_t=NOON - 100, robot="arm-2")
    assert assess(motion_rows(NOON - 50, moving(6), robot="arm-2", claimed_by=item.owner, item=item.id),
                  queue.items(), NOON) == []
