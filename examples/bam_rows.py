"""Worked example: one lab's existing in-house activity monitor, mapped onto `robot_activity.Sample`.

The lab already sampled its arm every 10 s with its own monitor (schema `bam-robot-activity/1`). Instead of replacing
that sampler, this adapter maps its rows, and the package's metrics then reproduce the monitor's own daily roll-up
(see `tests/test_real_parity.py`). Any program with a sampler of its own can follow the same pattern: write a
`to_samples` for your rows, and everything downstream (hours, tables, dashboard, W&B, the agent) works unchanged.

The mapping (in-house row -> Sample):

- `t`, `robot`, `sample_s` pass through.
- `claimed_by` = `owner` when `claimed`, else None.
- `item` = the job id while the monitor says a job runs (`kind == "job"`): activity is exactly the job time.
- `moving` passes through as True / False / None (None = no joint feed: unknown, never idle).
- `health` = the monitor's `state` when that is a fault state, else "ok".
- `reason` = the monitor's own `idle_reason`, verbatim: it already folds wait tags, decision waits, failed jobs and the
  queue state into one label, so it wins rung 1 of `classify()`.
- `queue` = the monitor's queue depth and runnable count; `pose` = `pose_deg` converted to radians (SI inside).

Usage, given the monitor's rows as dicts:

    samples = to_samples(rows)
    hours(samples), owners(samples), why_still(samples)
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from robot_activity.schema import QueueSnapshot, Sample

IN_HOUSE_SCHEMA = "bam-robot-activity/1"
FAULT_STATES = ("disconnected", "fault")


def to_samples(rows: Iterable[dict]) -> list[Sample]:
    """One Sample per in-house row, in the order given."""
    return [to_sample(row) for row in rows]


def to_sample(row: dict) -> Sample:
    return Sample(
        t=float(row["t"]),
        robot=str(row["robot"]),
        sample_s=float(row.get("sample_s") or 10.0),
        claimed_by=row.get("owner") if row.get("claimed") else None,
        item=row.get("job") if row.get("kind") == "job" else None,
        moving=row.get("moving"),
        health=row["state"] if row.get("state") in FAULT_STATES else "ok",
        reason=row.get("idle_reason"),
        queue=_queue(row),
        pose=_pose(row.get("pose_deg")),
    )


def _queue(row: dict) -> QueueSnapshot | None:
    depth, runnable = row.get("queue_depth"), row.get("runnable")
    if depth is None or runnable is None:
        return None
    return QueueSnapshot(depth=int(depth), runnable=int(runnable), running=row.get("queue_job"))


def _pose(pose_deg: dict | None) -> dict[str, float] | None:
    """The monitor stores degrees for people; the package keeps SI inside."""
    if not pose_deg:
        return None
    return {joint: math.radians(float(angle)) for joint, angle in pose_deg.items() if angle is not None}
