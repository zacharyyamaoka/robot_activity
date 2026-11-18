"""The row format: one `Sample` per robot per sampling window, written as one JSON line a human can read.

Positions are SI (rad / m). A field whose value is None may be left out of the JSON; a reader treats a missing key as
None and ignores keys it does not know, so newer samplers and older readers keep working together.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SCHEMA = "robot-activity/1"


@dataclass(frozen=True, kw_only=True)
class QueueSnapshot:
    """The robot's queue at the end of a window, so a dead row can say whether work was waiting."""

    depth: int                       # queued items for this robot (status "queued", robot or "any")
    runnable: int                    # of those, ready to start now
    running: str | None = None       # id of the running item, if any
    top_blocker: dict | None = None  # {"item", "owner", "kind", "why"} of the highest-priority not-ready item

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {"depth": self.depth, "runnable": self.runnable}
        if self.running is not None:
            record["running"] = self.running
        if self.top_blocker is not None:
            record["top_blocker"] = dict(self.top_blocker)
        return record

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> QueueSnapshot:
        top_blocker = record.get("top_blocker")
        return cls(depth=int(record.get("depth", 0)), runnable=int(record.get("runnable", 0)),
                   running=record.get("running"), top_blocker=dict(top_blocker) if top_blocker is not None else None)


@dataclass(frozen=True, kw_only=True)
class Sample:
    """One robot over one window ending at `t`: who held it, what ran, whether it moved, and why not."""

    t: float                              # window end, unix seconds
    robot: str
    sample_s: float = 10.0                # window length: the tracked seconds this row stands for
    claimed_by: str | None = None         # owner / agent holding the robot
    item: str | None = None               # queue item id dispatched on the robot
    moving: bool | None = None            # None = no joint feed: unknown, never idle
    health: str = "ok"                    # anything but "ok" (fault, disconnected, estop, ...) is dead
    reason: str | None = None             # explicit why; overrides the derived reason
    queue: QueueSnapshot | None = None
    pose: dict[str, float] | None = None  # joint positions at window end (SI), for the looping rule

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {"schema": SCHEMA, "t": self.t, "robot": self.robot, "sample_s": self.sample_s}
        optional = {"claimed_by": self.claimed_by, "item": self.item, "moving": self.moving}
        record.update({key: value for key, value in optional.items() if value is not None})
        record["health"] = self.health
        if self.reason is not None:
            record["reason"] = self.reason
        if self.queue is not None:
            record["queue"] = self.queue.to_dict()
        if self.pose is not None:
            record["pose"] = dict(self.pose)
        return record

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> Sample:
        queue = record.get("queue")
        pose = record.get("pose")
        return cls(
            t=float(record["t"]),
            robot=str(record["robot"]),
            sample_s=float(record.get("sample_s", 10.0)),
            claimed_by=record.get("claimed_by"),
            item=record.get("item"),
            moving=record.get("moving"),
            health=record.get("health") or "ok",
            reason=record.get("reason"),
            queue=QueueSnapshot.from_dict(queue) if queue is not None else None,
            pose={str(joint): float(position) for joint, position in pose.items()} if pose is not None else None,
        )
