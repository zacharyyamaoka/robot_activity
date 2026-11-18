"""Write one Sample per robot per window from a program's own probe.

The probe is the only program-specific part: a function that says who holds the robot, what item runs, its health and
(optionally) its joints right now. High-rate joint updates can be fed in between steps; motion is folded over the
window. With a queue, each row also carries the queue snapshot, and the running item fills in when the probe is silent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from robot_activity.motion import MotionDetector
from robot_activity.queue import Queue
from robot_activity.schema import Sample
from robot_activity.store import Store


@dataclass(frozen=True, kw_only=True)
class Observation:
    """What a program's probe reports about one robot right now."""

    claimed_by: str | None = None
    item: str | None = None
    joints: Any = None      # robot_msgs JointState or Mapping[str, float] (SI)
    health: str = "ok"
    reason: str | None = None


class Sampler:
    def __init__(self, store: Store, robot: str, probe: Callable[[], Observation], queue: Queue | None = None,
                 sample_s: float = 10.0, motion: MotionDetector | None = None):
        self.store = store
        self.robot = robot
        self.probe = probe
        self.queue = queue
        self.sample_s = float(sample_s)
        self.motion = motion if motion is not None else MotionDetector(window_s=self.sample_s)

    def feed(self, t: float, joints: Any) -> None:
        """A joint update between steps (any rate); folded into the next step's motion."""
        self.motion.update(t, joints)

    def step(self, now: float) -> Sample:
        """Probe, fold joints, write one Sample and return it. A probe that raises writes nothing (untracked)."""
        observation = self.probe()
        if observation.joints is not None:
            self.motion.update(now, observation.joints)
        moving = self.motion.moving(now)
        item, snapshot = observation.item, None
        if self.queue is not None:
            snapshot = self.queue.snapshot(self.robot)
            if item is None:
                item = snapshot.running
        sample = Sample(t=float(now), robot=self.robot, sample_s=self.sample_s, claimed_by=observation.claimed_by,
                        item=item, moving=moving, health=observation.health, reason=observation.reason,
                        queue=snapshot, pose=self.motion.pose() if moving is not None else None)
        self.store.append(sample)
        return sample
