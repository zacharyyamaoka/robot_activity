"""motion.py: MotionDetector over a robot_msgs JointState or a plain mapping (SPEC "motion.py")."""

from __future__ import annotations

import math

from robot_msgs.sensor import JointState

from robot_activity.motion import MotionDetector


def test_default_threshold_is_0_3_degrees():
    below = MotionDetector()
    below.update(0.0, {"J1": 0.0})
    below.update(5.0, {"J1": math.radians(0.29)})
    assert below.moving(5.0) is False

    above = MotionDetector()
    above.update(0.0, {"J1": 0.0})
    above.update(5.0, {"J1": math.radians(0.31)})
    assert above.moving(5.0) is True


def test_threshold_edge_is_strictly_greater():
    detector = MotionDetector(threshold=0.01)
    detector.update(1.0, {"J1": 0.0})
    detector.update(2.0, {"J1": 0.01})
    assert detector.moving(2.0) is False  # max - min == threshold is not motion

    detector.update(3.0, {"J1": 0.0100001})
    assert detector.moving(3.0) is True


def test_any_joint_moving_is_motion():
    detector = MotionDetector(threshold=0.01)
    detector.update(1.0, {"J1": 0.5, "J2": 1.0})
    detector.update(2.0, {"J1": 0.5, "J2": 1.2})
    assert detector.moving(2.0) is True


def test_none_without_updates():
    assert MotionDetector().moving(100.0) is None


def test_window_is_open_on_the_left_closed_on_the_right():
    detector = MotionDetector(threshold=0.01, window_s=10.0)
    detector.update(0.0, {"J1": 0.0})
    detector.update(5.0, {"J1": 1.0})
    assert detector.moving(5.0) is True     # (-5, 5] holds both updates
    assert detector.moving(10.0) is False   # (0, 10] holds only t=5: one reading, no range
    assert detector.moving(14.9) is False   # (4.9, 14.9] still holds t=5
    assert detector.moving(15.0) is None    # (5, 15] holds nothing


def test_range_only_over_the_window():
    detector = MotionDetector(threshold=0.01, window_s=10.0)
    detector.update(0.0, {"J1": 0.0})
    detector.update(20.0, {"J1": 1.0})
    assert detector.moving(20.0) is False  # the old reading at t=0 is outside (10, 20]


def test_joint_state_input():
    detector = MotionDetector()
    detector.update(0.0, JointState(name=("J1", "J2"), position=[0.0, 0.0]))
    detector.update(4.0, JointState(name=("J1", "J2"), position=[0.0, 0.02]))
    assert detector.moving(4.0) is True
    assert detector.pose() == {"J1": 0.0, "J2": 0.02}
    assert all(isinstance(value, float) for value in detector.pose().values())


def test_joint_state_still():
    detector = MotionDetector()
    detector.update(0.0, JointState(name=("J1",), position=[1.5]))
    detector.update(4.0, JointState(name=("J1",), position=[1.5 + math.radians(0.1)]))
    assert detector.moving(4.0) is False


def test_pose_is_latest_positions():
    detector = MotionDetector()
    assert detector.pose() is None
    detector.update(1.0, {"J1": 0.1, "J2": 0.2})
    detector.update(2.0, {"J1": 0.3, "J2": 0.4})
    assert detector.pose() == {"J1": 0.3, "J2": 0.4}
