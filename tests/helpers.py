"""Shared builders: hand-built rows with round numbers, so every expected hour in the tests is exact."""

from __future__ import annotations

import datetime
import math
from pathlib import Path

from robot_activity.queue import Item, PolicyRef, Queue
from robot_activity.schema import QueueSnapshot, Sample
from robot_activity.store import Store

DAY = "2026-11-18"
DAY_START = datetime.datetime(2026, 11, 18, tzinfo=datetime.timezone.utc).timestamp()  # 00:00 UTC
NOON = DAY_START + 12 * 3600

__all__ = ["DAY", "DAY_START", "NOON", "QueueSnapshot", "deg", "motion_rows", "policy", "sample", "series",
           "start_running", "submit_many", "write"]


def deg(value_deg: float) -> float:
    """Degrees to radians: thresholds and poses are SI inside the package."""
    return math.radians(value_deg)


def sample(t: float, *, robot: str = "arm-1", claimed_by: str | None = None, item: str | None = None,
           moving: bool | None = None, health: str = "ok", reason: str | None = None,
           queue: QueueSnapshot | None = None, pose: dict[str, float] | None = None, sample_s: float = 10.0) -> Sample:
    return Sample(t=float(t), robot=robot, sample_s=float(sample_s), claimed_by=claimed_by, item=item, moving=moving,
                  health=health, reason=reason, queue=queue, pose=pose)


def series(first_t: float, last_t: float, *, step: float = 10.0, **fields) -> list[Sample]:
    """One row every `step` seconds from first_t to last_t inclusive, each with sample_s = step."""
    count = int(round((last_t - first_t) / step)) + 1
    return [sample(first_t + index * step, sample_s=step, **fields) for index in range(count)]


def motion_rows(first_t: float, pattern: list[tuple[bool | None, dict[str, float] | None]], **fields) -> list[Sample]:
    """One 10 s row per (moving, pose) entry, starting at first_t."""
    return [sample(first_t + 10.0 * index, moving=moving, pose=pose, **fields) for index, (moving, pose) in enumerate(pattern)]


def write(store: Store, rows: list[Sample]) -> None:
    for row in rows:
        store.append(row)


def policy(name: str = "pick", *, entrypoint: str | None = None) -> PolicyRef:
    return PolicyRef(repo=f"https://github.com/example/{name}-policies", commit="0123456789abcdef0123456789abcdef01234567",
                     path=f"policies/{name}.py", entrypoint=entrypoint)


def start_running(queue: Queue, *, started_t: float, est_min: float = 60.0, robot: str = "arm-1",
                  owner: str = "pnp-agent", title: str = "pick vial") -> Item:
    """A ready item, started on `robot` at started_t by its owner."""
    item = queue.submit(title=title, owner=owner, est_min=est_min, robot=robot, policy=policy(), now=started_t - 10.0)
    return queue.start(item.id, robot=robot, by=owner, now=started_t)


def submit_many(root: str, prefix: str, count: int, start_event) -> None:
    """Concurrency worker (runs in a spawned process): wait for the shared start, then submit `count` items."""
    start_event.wait(60)
    queue = Queue(Path(root))
    for index in range(count):
        queue.submit(title=f"{prefix}-{index}", owner=prefix, est_min=1.0, policy=policy())
