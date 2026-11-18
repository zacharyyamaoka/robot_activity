"""Real-robot parity: this package's numbers against one lab's in-house monitor, on one real day (independent oracle).

The oracle is BAM's own activity monitor (`bam_robot_activity`, private, not imported here). Its rows for robot
`bam_fb_v2` (schema `bam-robot-activity/1`, one row per 10 s) were rolled up by ITS OWN code -
`wandb_log.daily_summary`, `rollup.minutes`, `rollup.owner_breakdown` and `reports._reason_table` ("Why it stood still
today") - and frozen as aggregates in `tests/data/bam_fb_v2-2026-11-17.oracle.json` by `tests/data/freeze_bam_oracle.py`
(kept for provenance; it records the in-house commit and the sha256 of the input file). This test maps the same raw rows
with `examples/bam_rows.py` `to_samples(rows)` and requires our `hours()`, `owners()` and `why_still()` to agree within
0.01 h. The in-house monitor's read-time corrections (backfill merge, test-source neutralisation, wait-tag annotations)
change nothing on this day (`day_rows_equal_file` in the oracle), so the raw file IS the oracle's input.

The mapping the oracle implies (in-house row -> Sample):
- `t`, `robot`, `sample_s` pass through.
- `claimed_by` = `owner` when `claimed` else None. (On this day `claimed` <=> `owner` present, so our owner table,
  grouped by claimed_by with "unclaimed" for None, groups exactly like the in-house `owner or "unclaimed"`.)
- `item` = the job id when in-house `kind == "job"`, else None: activity <=> kind == "job" (every job row is claimed and
  healthy on this day, so activity_h is the in-house job hours).
- `moving` passes through: True / False / None. The in-house rollup counts `is_active` (moving True, or a busy goal
  station); bam_fb_v2 rows carry no `goal`, so that is moving True. None (no joint feed) is unknown in both.
- `health` = the in-house `state` when it is "disconnected" or "fault", else "ok" (no such rows on this day).
- `reason` = the in-house `idle_reason`, verbatim (the in-house sampler already folds wait tags, decision waits, a failed
  job and the queue state into idle_reason), None when absent. Every still row on this day has one.
- `queue` (optional) = QueueSnapshot(depth=queue_depth, runnable=runnable); `pose` (optional) = pose_deg in radians.

Definitional differences and how the parity scope handles them:
- "claimed-still" (8.17 h) is the in-house fallback label for a claimed, still row with no other stated cause. 217 of
  those rows (0.60 h) are still INSIDE a running job: our own ladder would call them activity, "in activity (still)".
  Because the in-house sampler writes the label into `idle_reason`, the mapping passes it as an explicit reason, which
  wins rung 1 of classify(), so why_still() reproduces the in-house table label for label. Our derived ladder itself is
  pinned by test_classify.py, not here.
- The in-house monitor has no activity/dead split and no dead-reason table: why_dead() and dead streaks have no oracle
  and are left out. Its idle streaks are runs of moving False (unknown breaks them) and its hand-off gaps come from its
  queue's own files, not the rows: not comparable to our dead streaks or our events ledger, left out.
- Day boundaries: the in-house day file is a host-local date (America/Vancouver, UTC-7 that day: BC's permanent
  daylight time, in tzdata 2026e and later). Our Store with that tz must put every row on 2026-11-17 - checked below.
- Precision: the in-house code rounds hours to 3 dp (headline, owners) and the reason table to 2 dp and whole-% shares;
  0.01 h covers that rounding and nothing more (one mis-mapped 10 s row is 0.0028 h, so a handful of mis-mapped rows
  of one kind fails).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from robot_activity.metrics import hours, owners, why_still
from robot_activity.schema import Sample
from robot_activity.store import Store

pytestmark = pytest.mark.archive

REPO = Path(__file__).resolve().parents[1]
ORACLE = json.loads((Path(__file__).resolve().parent / "data" / "bam_fb_v2-2026-11-17.oracle.json").read_text())
ROWS_PATH = Path(os.environ.get("ROBOT_ACTIVITY_BAM_ROWS", ORACLE["input"]))
TOLERANCE_H = ORACLE["tolerance_h"]


def load_to_samples():
    spec = importlib.util.spec_from_file_location("bam_rows", REPO / "examples" / "bam_rows.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.to_samples


@pytest.fixture(scope="module")
def samples() -> list[Sample]:
    if not ROWS_PATH.is_file():
        pytest.skip(f"the lab's real rows are not on this machine ({ROWS_PATH})")
    data = ROWS_PATH.read_bytes()
    if hashlib.sha256(data).hexdigest() != ORACLE["input_sha256"]:
        pytest.skip("the real rows changed since the oracle was frozen; re-run tests/data/freeze_bam_oracle.py")
    rows = []
    for line in data.decode().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    rows.sort(key=lambda row: row["t"])
    return load_to_samples()(rows)


def test_every_row_maps_to_one_sample(samples):
    assert len(samples) == ORACLE["input_rows"]
    assert all(isinstance(row, Sample) for row in samples)
    assert {row.robot for row in samples} == {"bam_fb_v2"}
    assert {row.sample_s for row in samples} == set(ORACLE["preconditions"]["sample_s_values"])


def test_headline_hours(samples):
    ours = hours(samples)
    for name, expected in ORACLE["hours"].items():
        assert getattr(ours, name) == pytest.approx(expected, abs=TOLERANCE_H), name


def test_owner_table(samples):
    ours = owners(samples)
    assert [entry.owner for entry in ours] == [entry["owner"] for entry in ORACLE["owners"]]
    for entry, expected in zip(ours, ORACLE["owners"]):
        for name in ("hours", "moving_h", "still_h", "unknown_h"):
            assert getattr(entry, name) == pytest.approx(expected[name], abs=TOLERANCE_H), (entry.owner, name)


def test_why_it_stood_still(samples):
    ours = why_still(samples)
    assert [entry.reason for entry in ours] == [entry["reason"] for entry in ORACLE["why_still"]]
    for entry, expected in zip(ours, ORACLE["why_still"]):
        assert entry.hours == pytest.approx(expected["hours"], abs=TOLERANCE_H), entry.reason
        assert entry.share == pytest.approx(expected["share"], abs=0.005 + 1e-9), entry.reason  # whole-% display


def test_local_day_matches_the_monitor(samples, tmp_path):
    store = Store(tmp_path, tz=ORACLE["tz"])
    assert {store.day_of(row.t) for row in samples} == {ORACLE["day"]}
