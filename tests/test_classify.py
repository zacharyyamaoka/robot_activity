"""classify.py: the three splits and the reason ladder, first match wins (SPEC "classify.py")."""

from __future__ import annotations

import pytest

from helpers import sample
from robot_activity.classify import Classified, classify
from robot_activity.schema import QueueSnapshot

BLOCKER = {"item": "job-20", "owner": "ik-agent", "kind": "code", "why": "IK refuses the bin pose"}


def classified(**fields) -> Classified:
    return classify(sample(1000.0, **fields))


# --- the splits -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("moving, motion", [(True, "moving"), (False, "still"), (None, "unknown")])
def test_motion_split(moving, motion):
    assert classified(claimed_by="alice", item="job-1", moving=moving).motion == motion


def test_claim_split():
    assert classified(claimed_by="alice").claimed is True
    assert classified(claimed_by=None).claimed is False


def test_activity_needs_claim_item_and_health():
    assert classified(claimed_by="alice", item="job-1", moving=False).activity is True
    assert classified(claimed_by=None, item="job-1", moving=True).activity is False      # not claimed
    assert classified(claimed_by="alice", item=None, moving=True).activity is False      # nothing dispatched
    assert classified(claimed_by="alice", item="job-1", moving=True, health="fault").activity is False


# --- rung 1: an explicit reason wins over everything ----------------------------------------------------------------

def test_explicit_reason_beats_health():
    result = classified(claimed_by="bob", item="job-2", moving=False, health="fault", reason="BUS DOWN (rig fault)")
    assert result == Classified(claimed=True, activity=False, motion="still", reason="BUS DOWN (rig fault)")


def test_explicit_reason_beats_queue_and_claim():
    result = classified(claimed_by=None, moving=False, queue=QueueSnapshot(depth=2, runnable=2), reason="lunch break")
    assert result.reason == "lunch break"


def test_explicit_reason_beats_activity():
    assert classified(claimed_by="alice", item="job-1", moving=False, reason="waiting on the belt").reason == "waiting on the belt"


# --- rung 2: health -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("health", ["fault", "disconnected", "estop"])
def test_health_string_is_the_reason(health):
    result = classified(claimed_by="alice", item="job-1", moving=None, health=health,
                        queue=QueueSnapshot(depth=1, runnable=1))
    assert result == Classified(claimed=True, activity=False, motion="unknown", reason=health)


# --- rung 3: activity -----------------------------------------------------------------------------------------------

def test_activity_still_is_in_activity():
    result = classified(claimed_by="alice", item="job-1", moving=False, queue=QueueSnapshot(depth=3, runnable=2))
    assert result == Classified(claimed=True, activity=True, motion="still", reason="in activity (still)")


def test_activity_moving_has_no_reason():
    assert classified(claimed_by="alice", item="job-1", moving=True).reason is None


def test_activity_unknown_has_no_reason():
    # SPEC ambiguity: the Classified comment says "None only when activity and moving", but ladder rung 3 says
    # "activity and unknown -> None". The ladder is the more specific rule; it is followed here.
    assert classified(claimed_by="alice", item="job-1", moving=None).reason is None


# --- rung 4: dead with a queue snapshot -----------------------------------------------------------------------------

@pytest.mark.parametrize("runnable", [1, 2, 7])
def test_queue_waiting(runnable):
    result = classified(claimed_by="alice", moving=False, queue=QueueSnapshot(depth=runnable + 1, runnable=runnable))
    assert result.reason == f"queue waiting ({runnable} runnable, not started)"
    assert result.activity is False


def test_queue_waiting_exact_text():
    assert classified(claimed_by="alice", moving=False, queue=QueueSnapshot(depth=1, runnable=1)).reason == (
        "queue waiting (1 runnable, not started)")


def test_queue_waiting_beats_blocker():
    result = classified(claimed_by="alice", moving=False, queue=QueueSnapshot(depth=3, runnable=1, top_blocker=BLOCKER))
    assert result.reason == "queue waiting (1 runnable, not started)"


def test_blocked_on_top_blocker():
    result = classified(claimed_by="alice", moving=False, queue=QueueSnapshot(depth=2, runnable=0, top_blocker=BLOCKER))
    assert result.reason == "blocked on code: IK refuses the bin pose"


def test_queue_empty():
    assert classified(claimed_by="alice", moving=False, queue=QueueSnapshot(depth=0, runnable=0)).reason == "queue empty"


def test_queue_rung_applies_to_unclaimed_rows_too():
    # literal reading: rung 4 is "dead with a queue snapshot", claimed or not, and it comes before rung 5
    assert classified(claimed_by=None, moving=True, queue=QueueSnapshot(depth=0, runnable=0)).reason == "queue empty"


def test_queue_with_depth_but_no_runnable_and_no_blocker_falls_through():
    result = classified(claimed_by="alice", moving=False, queue=QueueSnapshot(depth=2, runnable=0, top_blocker=None))
    assert result.reason == "claimed, nothing dispatched"


# --- rung 5: claim ---------------------------------------------------------------------------------------------------

def test_claimed_nothing_dispatched():
    result = classified(claimed_by="alice", moving=False)
    assert result == Classified(claimed=True, activity=False, motion="still", reason="claimed, nothing dispatched")


def test_unclaimed():
    result = classified(claimed_by=None, moving=None)
    assert result == Classified(claimed=False, activity=False, motion="unknown", reason="unclaimed")


def test_item_without_claim_is_unclaimed():
    result = classified(claimed_by=None, item="job-1", moving=True)
    assert result == Classified(claimed=False, activity=False, motion="moving", reason="unclaimed")
