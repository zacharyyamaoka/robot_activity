"""The active queue on one screen: what runs now, the runnable order, then every not-ready item with WHY, owner, ETA.

Plain text, no ANSI escapes (a watcher may clear the screen around it). Times are relative to `now`, so the screen reads
the same in any time zone.
"""

from __future__ import annotations

import time

from robot_activity.queue import Item, Queue


def render_queue(queue: Queue, robot: str | None = None, now: float | None = None) -> str:
    now = time.time() if now is None else now
    scope = robot or "all robots"
    running = _running(queue, robot)
    runnable = _ordered(queue, robot, ready=True)
    waiting = _ordered(queue, robot, ready=False)
    lines = [f"QUEUE · {scope} · {len(running)} running · {len(runnable)} runnable · {len(waiting)} not ready", ""]

    lines.append("RUNNING NOW")
    lines.extend(_running_line(item, now) for item in running)
    if not running:
        lines.append("  (nothing)")

    lines += ["", "RUNNABLE · starts in this order (priority, then first submitted)"]
    lines.extend(_runnable_line(position, item, robot is None) for position, item in enumerate(runnable, start=1))
    if not runnable:
        lines.append("  (none: the queue is dry)")

    lines += ["", "NOT READY · why, owner, ETA"]
    for item in waiting:
        lines.append(f"  {item.title}  [{item.id}]  owner {item.owner}  {_robot(item, robot is None)}"
                     f"ETA {_eta(item, now)}")
        lines.extend(f"      why: {reason}" for reason in queue.why_not_ready(item.id))
    if not waiting:
        lines.append("  (none)")
    return "\n".join(lines) + "\n"


def _running(queue: Queue, robot: str | None) -> list[Item]:
    if robot is not None:
        item = queue.running(robot)
        return [item] if item else []
    return sorted(queue.items(status="running"), key=lambda item: item.ran_on or "")


def _ordered(queue: Queue, robot: str | None, *, ready: bool) -> list[Item]:
    if robot is not None:
        return queue.runnable(robot) if ready else queue.not_ready(robot)
    queued = queue.items(status="queued")
    return sorted((item for item in queued if queue.is_ready(item.id) == ready), key=lambda item: item.order)


def _running_line(item: Item, now: float) -> str:
    elapsed_min = (now - item.started_t) / 60 if item.started_t is not None else 0.0
    over = "  OVER ESTIMATE" if elapsed_min > item.est_min else ""
    return (f"  {item.ran_on}: {item.title}  [{item.id}]  owner {item.owner}  "
            f"{elapsed_min:.0f} of est {item.est_min:g} min{over}")


def _runnable_line(position: int, item: Item, show_robot: bool) -> str:
    policy = item.policy.describe() if item.policy else "-"
    return (f"  {position}. {item.title}  [{item.id}]  {item.source} p{item.priority}  owner {item.owner}  "
            f"{_robot(item, show_robot)}est {item.est_min:g} min  policy {policy}")


def _robot(item: Item, show: bool) -> str:
    return f"robot {item.robot}  " if show else ""


def _eta(item: Item, now: float) -> str:
    etas = [eta for eta in (item.eta_t, *(blocker.eta_t for blocker in item.blockers)) if eta is not None]
    if not etas:
        return "not given"
    eta = max(etas)
    minutes = (eta - now) / 60
    return f"in {minutes:.0f} min" if minutes >= 0 else f"{-minutes:.0f} min overdue"
