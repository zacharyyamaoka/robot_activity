"""Weights & Biases: one run per robot per day (`<robot>-<day>`), one run for the fleet (`fleet-<day>`).

`payload` and `fleet_payload` are pure and never import wandb, so they can be tested and inspected anywhere. `push`
(extra `[wandb]`) imports wandb inside the call, resumes each run by id, logs only the minutes it has not logged yet
(step = minute of the day), and rewrites the summary and tables, so pushing twice changes nothing.

W&B is a copy of the local files, never the source of truth. Hours moving is a proxy, not an objective: every run says
so in its notes.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from robot_activity.metrics import (
    STREAK_KINDS,
    fleet,
    handoff_gaps,
    hours,
    minutes,
    owners,
    streaks,
    why_dead,
    why_still,
)
from robot_activity.queue import Queue
from robot_activity.schema import Sample
from robot_activity.store import Store

CAVEAT = ("Hours moving is a proxy, not an objective (Goodhart). Some idle time is slack a bottleneck needs (Theory of "
          "Constraints); a throughput KPI such as useful picks per day is the counterweight.")


def payload(store: Store, queue: Queue, robot: str, day: str) -> dict[str, Any]:
    rows = store.rows(robot, day)
    gaps = [gap for gap in handoff_gaps(queue.events(), robot) if store.day_of(gap["ended_t"]) == day]
    return {
        "run_id": f"{robot}-{day}",
        "steps": minutes(rows),
        "summary": asdict(hours(rows)),
        "tables": {
            "why_still": [asdict(entry) for entry in why_still(rows)],
            "owners": [asdict(entry) for entry in owners(rows)],
            "streaks": [asdict(streak) for kind in STREAK_KINDS for streak in streaks(rows, kind)],
            "handoff_gaps": gaps,
        },
    }


def fleet_payload(store: Store, day: str) -> dict[str, Any]:
    rows_by_robot = {robot: rows for robot in store.robots() if (rows := store.rows(robot, day))}
    now = max((rows[-1].t for rows in rows_by_robot.values()), default=store.day_bounds(day)[0])
    all_rows = [row for rows in rows_by_robot.values() for row in rows]
    return {
        "run_id": f"fleet-{day}",
        "steps": _fleet_minutes(rows_by_robot),
        "summary": asdict(fleet(rows_by_robot, now)),
        "tables": {
            "robots": [{"robot": robot, **asdict(hours(rows))} for robot, rows in rows_by_robot.items()],
            "why_still": [asdict(entry) for entry in why_still(all_rows)],
            "why_dead": [asdict(entry) for entry in why_dead(all_rows)],
            "owners": [asdict(entry) for entry in owners(all_rows)],
        },
    }


def _fleet_minutes(rows_by_robot: dict[str, list[Sample]]) -> list[dict[str, Any]]:
    """Per minute across robots: robots sampled, and the fleet's activity and moving fractions."""
    merged: dict[int, dict[str, float]] = {}
    for rows in rows_by_robot.values():
        for minute in minutes(rows):
            entry = merged.setdefault(minute["minute_t"], {"robots_sampled": 0, "sampled_s": 0.0, "activity_s": 0.0,
                                                           "moving_s": 0.0})
            entry["robots_sampled"] += 1
            entry["sampled_s"] += minute["sampled_s"]
            entry["activity_s"] += minute["activity_frac"] * minute["sampled_s"]
            entry["moving_s"] += minute["moving_frac"] * minute["sampled_s"]
    return [{"minute_t": minute_t, "robots_sampled": entry["robots_sampled"], "sampled_s": entry["sampled_s"],
             "activity_frac": entry["activity_s"] / entry["sampled_s"] if entry["sampled_s"] else 0.0,
             "moving_frac": entry["moving_s"] / entry["sampled_s"] if entry["sampled_s"] else 0.0}
            for minute_t, entry in sorted(merged.items())]


# --- push (needs the [wandb] extra) ---------------------------------------------------------------------------------

def push(store: Store, queue: Queue, day: str, project: str, entity: str | None = None) -> dict[str, Any]:
    """Log every robot's day and the fleet's day. Idempotent: a second push logs no new steps."""
    import wandb  # WHY here: wandb is an extra; `import robot_activity` must never pull it in

    api = wandb.Api()
    entity = entity or api.default_entity
    day_start, _ = store.day_bounds(day)
    local_dir = store.root / ".wandb"  # WHY: run files are a cache of the push, never inside a source checkout
    local_dir.mkdir(parents=True, exist_ok=True)
    runs = []
    for robot in store.robots():
        robot_payload = payload(store, queue, robot, day)
        if robot_payload["steps"]:
            live = _is_live(store.rows(robot, day))
            runs.append(_push_run(wandb, api, entity, project, robot_payload, day_start, local_dir, live,
                                  job_type="robot-day", config={"robot": robot, "day": day, "tz": store.tz}))
    fleet_run = fleet_payload(store, day)
    if fleet_run["steps"]:
        live = any(_is_live(store.rows(robot, day)) for robot in store.robots())
        runs.append(_push_run(wandb, api, entity, project, fleet_run, day_start, local_dir, live,
                              job_type="fleet-day", config={"robots": len(fleet_run["tables"]["robots"]), "day": day,
                                                            "tz": store.tz}))
    return {"entity": entity, "project": project, "url": f"https://wandb.ai/{entity}/{project}", "runs": runs}


def _is_live(rows: list[Sample]) -> bool:
    """A sampler wrote the last row moments ago: its newest minute is still filling and must not be logged yet."""
    return bool(rows) and 0 <= time.time() - rows[-1].t <= 2 * rows[-1].sample_s


def _last_logged_minute(api: Any, path: str) -> float | None:
    try:
        return api.run(path).summary.get("last_minute_t")
    except Exception:  # noqa: BLE001 - a run that does not exist yet (wandb raises its own CommError / ValueError)
        return None


def _push_run(wandb: Any, api: Any, entity: str, project: str, run_payload: dict[str, Any], day_start: float,
              local_dir: Path, live: bool, *, job_type: str, config: dict[str, Any]) -> dict[str, Any]:
    run_id = run_payload["run_id"]
    last = _last_logged_minute(api, f"{entity}/{project}/{run_id}")
    steps = [step for step in run_payload["steps"] if last is None or step["minute_t"] > last]
    if live and steps:
        steps = steps[:-1]  # WHY: steps never rewind, so a partial minute logged now would stay partial forever
    run = wandb.init(entity=entity, project=project, id=run_id, name=run_id, resume="allow", job_type=job_type,
                     group=config.get("day"), notes=CAVEAT, config=config, dir=str(local_dir),
                     tags=["robot-activity", job_type], settings=wandb.Settings(silent=True, console="off"))
    run.define_metric("hour")
    run.define_metric("*", step_metric="hour")
    for step in steps:
        minute_of_day = int((step["minute_t"] - day_start) // 60)
        record = {key: value for key, value in step.items() if value is not None}
        record["hour"] = minute_of_day / 60.0
        run.log(record, step=minute_of_day)
    run.summary.update(run_payload["summary"])
    if steps:
        run.summary["last_minute_t"] = steps[-1]["minute_t"]
    for name, rows in run_payload["tables"].items():
        run.summary[f"table/{name}"] = _table(wandb, rows)
    url = run.url
    run.finish()
    return {"run_id": run_id, "url": url, "logged": len(steps)}


def _table(wandb: Any, rows: list[dict[str, Any]]) -> Any:
    columns = list(rows[0]) if rows else ["empty"]
    data = [[_cell(row.get(column)) for column in columns] for row in rows]
    return wandb.Table(columns=columns, data=data)


def _cell(value: Any) -> Any:
    if isinstance(value, dict):
        return ", ".join(f"{key}: {amount / 60:.1f} min" if isinstance(amount, float) else f"{key}: {amount}"
                         for key, amount in sorted(value.items(), key=lambda entry: -float(entry[1] or 0)))
    return value
