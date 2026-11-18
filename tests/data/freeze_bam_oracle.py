"""Freeze the independent oracle for tests/test_real_parity.py: one real day of one lab's in-house activity monitor, rolled up by
THAT monitor's own code, never by robot_activity.

Provenance. BAM runs an in-house monitor (package `bam_robot_activity`, private) that writes one `bam-robot-activity/1` row per
robot per 10 s. Its own rollups are what its people read: `rollup.owner_breakdown` ("Who held the robot"),
`reports._reason_table` ("Why it stood still today"), `wandb_log.daily_summary` and `rollup.minutes` (the day's headline hours).
This script calls exactly those functions on `rollup.day_rows(<bam_fb_v2>, DAY)` and writes the aggregates to
`bam_fb_v2-2026-11-17.oracle.json` beside it. Aggregates only: no raw rows, no command lines, no paths beyond the one input.

Run it with the in-house monitor's own interpreter (it imports `bam_robot_activity` and, for `reports`, wandb_workspaces);
it reads the archive and writes only the oracle JSON:

    WANDB_MODE=disabled <bam_robot_activity venv>/bin/python -I tests/data/freeze_bam_oracle.py

Frozen 2026-11-18 from the in-house code at the commit recorded in the JSON (`in_house_commit`).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from bam_robot_activity import reports, rollup, wandb_log
from bam_robot_activity.robots import FLEET

ROBOT = "bam_fb_v2"
DAY = "2026-11-17"
OUT = Path(__file__).resolve().parent / f"{ROBOT}-{DAY}.oracle.json"


def file_rows(path: Path) -> list[dict]:
    """The day file as written (what the parity test reads), parsed the in-house way."""
    return rollup.read_rows(path)


def reason_table(rows: list[dict]) -> tuple[list[dict], float]:
    """`reports._reason_table` renders markdown (hours to 2 dp, share to whole %); parse its own output back into rows."""
    markdown, still_h = reports._reason_table(rows)
    table = []
    for line in markdown.splitlines()[2:]:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        reason, hours, share = cells
        table.append({"reason": reason, "hours": float(hours), "share": float(share.rstrip("%")) / 100.0})
    return table, still_h


def main() -> int:
    robot = FLEET[ROBOT]
    input_path = robot.out_dir() / f"{DAY}.jsonl"
    rows = rollup.day_rows(robot, DAY)
    # The parity test feeds the raw day file to examples/bam_rows.py. That is only the same input when the monitor's own
    # read-time corrections (backfill merge, test-source neutralisation, wait-tag annotations) changed nothing on this day.
    as_written = file_rows(input_path)
    day_rows_equal_file = len(rows) == len(as_written) and all(a == b for a, b in zip(rows, as_written))
    if not day_rows_equal_file:
        print("day_rows differs from the file as written: the parity input would not be the oracle's input", file=sys.stderr)
        return 1

    per_minute = rollup.minutes(rows)
    summary = wandb_log.daily_summary(rows, per_minute)
    reasons, still_h = reason_table(rows)
    unknown_s = sum(minute["unknown_s"] for minute in per_minute)
    job_s = sum(minute["job_frac"] * minute["sampled_s"] for minute in per_minute)  # kind == "job", per minute (3 dp frac)
    owners = [{key: entry[key] for key in ("owner", "hours", "moving_h", "still_h", "unknown_h")}
              for entry in rollup.owner_breakdown(rows)]

    # Preconditions the parity mapping relies on, recorded rather than assumed (aggregates, not rows):
    kinds = Counter(row.get("kind") for row in rows)
    states = Counter(row.get("state") for row in rows)
    preconditions = {
        "every_job_row_claimed": all(row["claimed"] for row in rows if row.get("kind") == "job"),
        "claimed_iff_owner": all(bool(row["claimed"]) == (row.get("owner") is not None) for row in rows),
        "still_rows_without_idle_reason": sum(1 for row in rows if row["moving"] is False and not row.get("idle_reason")),
        "fault_or_disconnected_rows": sum(1 for row in rows if row.get("state") in ("fault", "disconnected")),
        "rows_with_goal": sum(1 for row in rows if row.get("goal") is not None),
        "sample_s_values": sorted({row.get("sample_s", 10.0) for row in rows}),
        "kinds": dict(sorted((str(kind), count) for kind, count in kinds.items())),
        "states": dict(sorted(states.items())),
    }

    package_dir = Path(rollup.__file__).resolve().parent
    commit = subprocess.run(["git", "-C", str(package_dir), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    oracle = {
        "schema": "robot-activity-oracle/1",
        "robot": ROBOT,
        "day": DAY,
        "tz": "America/Vancouver",  # the monitor's day files are local dates on its host
        "input": str(input_path),
        "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "input_rows": len(rows),
        "in_house_schema": rows[0]["schema"],
        "in_house_commit": commit if re.fullmatch(r"[0-9a-f]{40}", commit) else None,
        "producers": {
            "hours.tracked_h / claimed_h / unclaimed_h / moving_h": "wandb_log.daily_summary (3 dp)",
            "hours.still_h": "reports._reason_table total (moving False)",
            "hours.unknown_h": "rollup.minutes unknown_s summed",
            "hours.activity_h": "rollup.minutes job_frac x sampled_s summed (kind == 'job')",
            "owners": "rollup.owner_breakdown (3 dp)",
            "why_still": "reports._reason_table, parsed (hours 2 dp, share whole %)",
        },
        "day_rows_equal_file": day_rows_equal_file,
        "preconditions": preconditions,
        "hours": {
            "tracked_h": summary["sampled_h"],
            "claimed_h": summary["claimed_h"],
            "unclaimed_h": summary["unclaimed_h"],
            "moving_h": summary["moving_h"],
            "still_h": round(still_h, 4),
            "unknown_h": round(unknown_s / 3600.0, 4),
            "activity_h": round(job_s / 3600.0, 4),
        },
        "owners": owners,
        "why_still": reasons,
        "tolerance_h": 0.01,
    }
    OUT.write_text(json.dumps(oracle, indent=2) + "\n")
    print(json.dumps(oracle["hours"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
