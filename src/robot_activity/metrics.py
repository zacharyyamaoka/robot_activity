"""Hours on the three splits, the tables that explain them, and the fleet roll-up.

Tracked time is the sum of `sample_s` over rows; a gap with no rows (sampler down) is untracked, never idle. Every
function here is pure: rows in, numbers out.

WHY keep this caveat next to the numbers: hours moving is a proxy, not an objective (Goodhart). Some idle time is slack
that a bottleneck needs (Theory of Constraints); a throughput KPI such as useful picks per day is the counterweight.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from robot_activity.classify import Classified, classify, label
from robot_activity.schema import Sample

STREAK_KINDS = ("activity", "dead", "moving")
CONSECUTIVE_FACTOR = 1.5   # rows are consecutive when t - previous t <= 1.5 * sample_s


@dataclass(frozen=True)
class Hours:
    tracked_h: float
    claimed_h: float
    unclaimed_h: float
    activity_h: float
    dead_h: float
    moving_h: float
    still_h: float
    unknown_h: float
    utilization: float      # activity_h / tracked_h (0.0 when nothing tracked): TEEP-style, all calendar time
    moving_share: float     # moving_h / tracked_h


@dataclass(frozen=True)
class ReasonHours:
    reason: str
    hours: float
    share: float            # share of the table's total


@dataclass(frozen=True)
class OwnerHours:
    owner: str
    hours: float
    moving_h: float
    still_h: float
    unknown_h: float


@dataclass(frozen=True)
class Streak:
    kind: str
    start_t: float          # first row's t - its sample_s
    end_t: float            # last row's t
    seconds: float          # tracked seconds in the streak (sum of sample_s)
    reasons: dict[str, float]


@dataclass(frozen=True)
class FleetSummary:
    robots: int
    robots_active_now: int
    robots_moving_now: int
    tracked_h: float
    activity_h: float
    moving_h: float
    utilization: float


def _ratio(part: float, whole: float) -> float:
    return part / whole if whole > 0 else 0.0


def hours(rows: Iterable[Sample]) -> Hours:
    seconds: dict[str, float] = defaultdict(float)
    for row in rows:
        classified = classify(row)
        seconds["tracked"] += row.sample_s
        seconds["claimed" if classified.claimed else "unclaimed"] += row.sample_s
        seconds["activity" if classified.activity else "dead"] += row.sample_s
        seconds[classified.motion] += row.sample_s
    tracked = seconds["tracked"]
    return Hours(
        tracked_h=tracked / 3600.0,
        claimed_h=seconds["claimed"] / 3600.0, unclaimed_h=seconds["unclaimed"] / 3600.0,
        activity_h=seconds["activity"] / 3600.0, dead_h=seconds["dead"] / 3600.0,
        moving_h=seconds["moving"] / 3600.0, still_h=seconds["still"] / 3600.0, unknown_h=seconds["unknown"] / 3600.0,
        utilization=_ratio(seconds["activity"], tracked), moving_share=_ratio(seconds["moving"], tracked))


def _reason_table(rows: Iterable[Sample], keep) -> list[ReasonHours]:
    seconds: dict[str, float] = defaultdict(float)
    for row in rows:
        classified = classify(row)
        if keep(classified):
            seconds[label(classified)] += row.sample_s
    total = sum(seconds.values())
    ranked = sorted(seconds.items(), key=lambda entry: (-entry[1], entry[0]))
    return [ReasonHours(reason=reason, hours=value / 3600.0, share=_ratio(value, total)) for reason, value in ranked]


def why_still(rows: Iterable[Sample]) -> list[ReasonHours]:
    """Still rows by reason, most hours first. Unknown rows are not still: no joint feed is never idle."""
    return _reason_table(rows, lambda classified: classified.motion == "still")


def why_dead(rows: Iterable[Sample]) -> list[ReasonHours]:
    """Dead rows (not activity) by reason, most hours first."""
    return _reason_table(rows, lambda classified: not classified.activity)


def owners(rows: Iterable[Sample]) -> list[OwnerHours]:
    """Who held the robot, by `claimed_by` ("unclaimed" for nobody), most hours first."""
    seconds: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        owner = row.claimed_by if row.claimed_by is not None else "unclaimed"
        seconds[owner]["total"] += row.sample_s
        seconds[owner][classify(row).motion] += row.sample_s
    table = [OwnerHours(owner=owner, hours=split["total"] / 3600.0, moving_h=split["moving"] / 3600.0,
                        still_h=split["still"] / 3600.0, unknown_h=split["unknown"] / 3600.0)
             for owner, split in seconds.items()]
    return sorted(table, key=lambda entry: (-entry.hours, entry.owner))


def _side(classified: Classified, kind: str) -> bool:
    if kind == "activity":
        return classified.activity
    if kind == "dead":
        return not classified.activity
    return classified.motion == "moving"


def streaks(rows: Iterable[Sample], kind: str) -> list[Streak]:
    """Maximal runs of consecutive rows on one side of a split ("activity", "dead" or "moving")."""
    if kind not in STREAK_KINDS:
        raise ValueError(f"streak kind {kind!r} is not one of {', '.join(STREAK_KINDS)}")
    found: list[Streak] = []
    run: list[tuple[Sample, Classified]] = []
    previous: Sample | None = None
    for row in sorted(rows, key=lambda sample: sample.t):
        classified = classify(row)
        consecutive = previous is not None and row.t - previous.t <= CONSECUTIVE_FACTOR * row.sample_s
        if run and (not consecutive or not _side(classified, kind)):
            found.append(_streak(kind, run))
            run = []
        if _side(classified, kind):
            run.append((row, classified))
        previous = row
    if run:
        found.append(_streak(kind, run))
    return found


def _streak(kind: str, run: list[tuple[Sample, Classified]]) -> Streak:
    reasons: dict[str, float] = defaultdict(float)
    for row, classified in run:
        reasons[label(classified)] += row.sample_s
    first, last = run[0][0], run[-1][0]
    return Streak(kind=kind, start_t=first.t - first.sample_s, end_t=last.t,
                  seconds=math.fsum(row.sample_s for row, _ in run), reasons=dict(reasons))


def handoff_gaps(events: list[dict], robot: str) -> list[dict]:
    """End of one item to the start of the next on the same robot, from the events ledger."""
    gaps = []
    last_finish: dict | None = None
    for event in sorted(events, key=lambda event: event["t"]):
        if event.get("robot") != robot:
            continue
        if event.get("kind") == "finish":
            last_finish = event
        elif event.get("kind") == "start" and last_finish is not None:
            gaps.append({"after": last_finish["id"], "next": event["id"], "ended_t": last_finish["t"],
                         "started_t": event["t"], "gap_s": event["t"] - last_finish["t"]})
            last_finish = None
    return gaps


def minutes(rows: Iterable[Sample]) -> list[dict]:
    """One record per wall-clock minute that has rows, in time order (the steps of a W&B run)."""
    by_minute: dict[int, list[Sample]] = defaultdict(list)
    for row in sorted(rows, key=lambda sample: sample.t):
        by_minute[int(row.t // 60) * 60].append(row)
    records = []
    for minute_t, minute_rows in sorted(by_minute.items()):
        seconds: dict[str, float] = defaultdict(float)
        for row in minute_rows:
            classified = classify(row)
            seconds["sampled"] += row.sample_s
            seconds["claimed"] += row.sample_s if classified.claimed else 0.0
            seconds["activity"] += row.sample_s if classified.activity else 0.0
            seconds[classified.motion] += row.sample_s
        sampled = seconds["sampled"]
        last_queue = minute_rows[-1].queue
        records.append({
            "minute_t": minute_t, "sampled_s": sampled,
            "claimed_frac": _ratio(seconds["claimed"], sampled), "activity_frac": _ratio(seconds["activity"], sampled),
            "moving_frac": _ratio(seconds["moving"], sampled), "unknown_frac": _ratio(seconds["unknown"], sampled),
            "queue_depth": last_queue.depth if last_queue else None,
            "runnable": last_queue.runnable if last_queue else None,
        })
    return records


def fleet(rows_by_robot: Mapping[str, list[Sample]], now: float, fresh_s: float = 60.0) -> FleetSummary:
    """Fleet totals, and how many robots are active / moving now (their latest row, if within fresh_s of now)."""
    all_rows = [row for rows in rows_by_robot.values() for row in rows]
    totals = hours(all_rows)
    active_now = moving_now = 0
    for rows in rows_by_robot.values():
        if not rows:
            continue
        latest = max(rows, key=lambda sample: sample.t)
        if now - latest.t <= fresh_s:
            active_now += classify(latest).activity
            moving_now += latest.moving is True
    return FleetSummary(robots=len(rows_by_robot), robots_active_now=active_now, robots_moving_now=moving_now,
                        tracked_h=totals.tracked_h, activity_h=totals.activity_h, moving_h=totals.moving_h,
                        utilization=totals.utilization)
