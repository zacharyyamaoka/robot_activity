"""Stalemates: activity that loops in place. Each rule is a proxy, a sign to look, never an objective.

- stuck:   a running item, and the robot still for the last >= still_limit_s of rows. Unknown rows (no joint feed)
           neither count as still nor reset the count.
- looping: within the last loop_window_s the robot came to rest (a still row right after a moving row) >= 3 times at
           <= 2 distinct poses (each joint rounded to pose_grid), and no item finished on it in that window. An item
           that ended without success (failed, cancelled) in the window was the owner's answer: rests before it
           no longer count.
- overrun: a running item past overrun_factor * est_min * 60 + overrun_slack_s seconds since it started.

WHY these exist: hours moving is a proxy that motion alone can fake. These rules are the guard against motion that
looks like work, so the item's owner and the dispatcher can open the shared policy and judge the behaviour.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise

from robot_activity.metrics import CONSECUTIVE_FACTOR
from robot_activity.queue import Item
from robot_activity.schema import Sample

LOOP_MIN_RESTS = 3
LOOP_MAX_POSES = 2


@dataclass(frozen=True)
class Flag:
    robot: str
    kind: str               # "stuck" | "looping" | "overrun"
    message: str
    item: str | None
    since_t: float


def assess(rows: list[Sample], items: list[Item], now: float, *, still_limit_s: float = 300.0,
           loop_window_s: float = 600.0, pose_grid: float = math.radians(2.0), overrun_factor: float = 1.5,
           overrun_slack_s: float = 120.0) -> list[Flag]:
    """Flags for every robot that has rows (at or before `now`)."""
    flags: list[Flag] = []
    for robot, robot_rows in _rows_by_robot(rows, now).items():
        running = next((item for item in items if item.status == "running" and item.ran_on == robot), None)
        if running is not None:
            flags.extend(_stuck(robot, robot_rows, running, still_limit_s))
            flags.extend(_overrun(robot, running, now, overrun_factor, overrun_slack_s))
        flags.extend(_looping(robot, robot_rows, items, running, now, loop_window_s, pose_grid))
    return flags


def _rows_by_robot(rows: Iterable[Sample], now: float) -> dict[str, list[Sample]]:
    grouped: dict[str, list[Sample]] = defaultdict(list)
    for row in sorted(rows, key=lambda sample: sample.t):
        if row.t <= now:
            grouped[row.robot].append(row)
    return grouped


def _stuck(robot: str, rows: list[Sample], running: Item, still_limit_s: float) -> list[Flag]:
    still_s, first_still = 0.0, None
    for row in reversed(rows):
        if row.moving is True or (running.started_t is not None and row.t <= running.started_t):
            break
        if row.moving is False:
            still_s += row.sample_s
            first_still = row
    if first_still is None or still_s < still_limit_s:
        return []
    message = (f"still for {still_s / 60:.0f} min inside running item {running.id} ({running.title}); "
               f"the limit is {still_limit_s / 60:g} min")
    return [Flag(robot=robot, kind="stuck", message=message, item=running.id,
                 since_t=first_still.t - first_still.sample_s)]


def _overrun(robot: str, running: Item, now: float, factor: float, slack_s: float) -> list[Flag]:
    if running.started_t is None:
        return []
    limit_s = factor * running.est_min * 60.0 + slack_s
    elapsed_s = now - running.started_t
    if elapsed_s <= limit_s:
        return []
    message = (f"{running.id} ({running.title}) has run {elapsed_s / 60:.0f} min against an estimate of "
               f"{running.est_min:g} min (limit {limit_s / 60:.0f} min)")
    return [Flag(robot=robot, kind="overrun", message=message, item=running.id, since_t=running.started_t + limit_s)]


def _looping(robot: str, rows: list[Sample], items: list[Item], running: Item | None, now: float,
             window_s: float, pose_grid: float) -> list[Flag]:
    window_start = now - window_s
    ended = [item for item in items if item.ran_on == robot and item.finished_t is not None
             and window_start < item.finished_t <= now]
    if any(item.status == "done" for item in ended):
        return []  # progress: something finished
    window_start = max([window_start, *(item.finished_t for item in ended)])
    rests = [row for previous, row in pairwise(rows)
             if row.t > window_start and row.moving is False and previous.moving is True and row.pose is not None
             and row.t - previous.t <= CONSECUTIVE_FACTOR * row.sample_s]
    cells = {_cell(rest.pose, pose_grid) for rest in rests}
    if len(rests) < LOOP_MIN_RESTS or len(cells) > LOOP_MAX_POSES:
        return []
    subject = f"while running {running.id} ({running.title})" if running else "with no item running"
    message = (f"came to rest {len(rests)} times in {window_s / 60:g} min at {len(cells)} pose(s) "
               f"({_describe_poses([rest.pose for rest in _distinct(rests, pose_grid)])}) {subject}, "
               f"and nothing finished")
    return [Flag(robot=robot, kind="looping", message=message, item=running.id if running else None,
                 since_t=rests[0].t)]


def _cell(pose: dict[str, float], grid: float) -> tuple[tuple[str, int], ...]:
    return tuple(sorted((joint, round(position / grid)) for joint, position in pose.items()))


def _distinct(rests: list[Sample], grid: float) -> list[Sample]:
    seen: dict[tuple, Sample] = {}
    for rest in rests:
        seen.setdefault(_cell(rest.pose, grid), rest)
    return list(seen.values())


def _describe_poses(poses: list[dict[str, float]]) -> str:
    """The joints that tell the rest poses apart, in degrees (display only; poses stay SI): "J2 17.8° / 6.9°"."""
    joints = [joint for joint in poses[0] if len({round(pose.get(joint, 0.0), 4) for pose in poses}) > 1]
    shown = joints or list(poses[0])[:3]
    return ", ".join(f"{joint} " + " / ".join(f"{math.degrees(pose[joint]):.1f}°" for pose in poses if joint in pose)
                     for joint in shown)
