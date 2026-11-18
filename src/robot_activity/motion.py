"""Is the robot moving? Any joint's range over the last window above a small threshold.

Feed it a `robot_msgs.sensor.JointState` or any mapping of joint name to position (SI: rad or m). No reading inside the
window means no joint feed: `moving()` returns None (unknown), never False, so a dead feed is never counted as idle.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from typing import Any


def joint_positions(joints: Any) -> dict[str, float]:
    """Joint name -> position from a robot_msgs JointState (duck-typed: `name` + `position`) or a mapping."""
    if isinstance(joints, Mapping):
        return {str(name): float(position) for name, position in joints.items()}
    names, positions = getattr(joints, "name", None), getattr(joints, "position", None)
    if names is None or positions is None:
        raise TypeError(f"expected a JointState or a mapping of joint positions, got {type(joints).__name__}")
    return {str(name): float(position) for name, position in zip(names, positions)}


class MotionDetector:
    """Moving when any joint's max - min over the window (now - window_s, now] exceeds `threshold`."""

    def __init__(self, threshold: float = math.radians(0.3), window_s: float = 10.0):
        self.threshold = threshold
        self.window_s = window_s
        self._readings: deque[tuple[float, dict[str, float]]] = deque()
        self._latest: dict[str, float] | None = None
        self._newest_t = -math.inf

    def update(self, t: float, joints: Any) -> None:
        positions = joint_positions(joints)
        self._readings.append((float(t), positions))
        self._latest = positions
        self._newest_t = max(self._newest_t, float(t))
        # readings older than one window before the newest can never fall inside a window again
        while self._readings and self._readings[0][0] <= self._newest_t - self.window_s:
            self._readings.popleft()

    def moving(self, now: float) -> bool | None:
        in_window = [positions for reading_t, positions in self._readings if now - self.window_s < reading_t <= now]
        if not in_window:
            return None
        ranges: dict[str, tuple[float, float]] = {}
        for positions in in_window:
            for joint, position in positions.items():
                low, high = ranges.get(joint, (position, position))
                ranges[joint] = (min(low, position), max(high, position))
        return any(high - low > self.threshold for low, high in ranges.values())

    def pose(self) -> dict[str, float] | None:
        return dict(self._latest) if self._latest is not None else None
