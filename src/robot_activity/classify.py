"""One row onto the three splits of tracked time, plus the reason it was not useful work.

- claim: someone (`claimed_by`) holds the robot, or nobody does.
- activity: claimed AND an item is dispatched AND health is "ok". Being still inside a running item is activity.
- motion: moving / still / unknown. Unknown means no joint feed and is never counted as idle.

The reason ladder is first-match-wins (SPEC "classify.py").
"""

from __future__ import annotations

from dataclasses import dataclass

from robot_activity.schema import Sample

MOTION = {True: "moving", False: "still", None: "unknown"}
IN_ACTIVITY_STILL = "in activity (still)"


@dataclass(frozen=True)
class Classified:
    claimed: bool
    activity: bool
    motion: str          # "moving" | "still" | "unknown"
    reason: str | None   # None only inside activity, while moving or with no joint feed


def classify(sample: Sample) -> Classified:
    claimed = sample.claimed_by is not None
    activity = claimed and sample.item is not None and sample.health == "ok"
    motion = MOTION[sample.moving]
    return Classified(claimed=claimed, activity=activity, motion=motion, reason=_reason(sample, activity, motion))


def label(classified: Classified) -> str:
    """The reason, or for activity rows that carry none, "in activity (<motion>)": a name for every row."""
    return classified.reason if classified.reason is not None else f"in activity ({classified.motion})"


def _reason(sample: Sample, activity: bool, motion: str) -> str | None:
    if sample.reason is not None:
        return sample.reason
    if sample.health != "ok":
        return sample.health
    if activity:
        # WHY: "sometimes being still is activity" - a robot paused inside its job is working, not idle.
        return IN_ACTIVITY_STILL if motion == "still" else None
    queue_reason = _queue_reason(sample)
    if queue_reason is not None:
        return queue_reason
    return "claimed, nothing dispatched" if sample.claimed_by is not None else "unclaimed"


def _queue_reason(sample: Sample) -> str | None:
    queue = sample.queue
    if queue is None:
        return None
    if queue.runnable > 0:
        return f"queue waiting ({queue.runnable} runnable, not started)"
    if queue.depth > 0 and queue.top_blocker:
        return f"blocked on {queue.top_blocker.get('kind', 'other')}: {queue.top_blocker.get('why', '')}"
    if queue.depth == 0:
        return "queue empty"
    return None
