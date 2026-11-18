"""The Robot Activity Agent's one loop: keep every robot doing useful work, and say why when it isn't.

Each `tick(now)`, for each robot that has rows:

1. dead for >= idle_after_s: prompt the executor of the next runnable item to start it (naming the item, its estimate
   and its policy), or, with nothing runnable, ask the owners of the top not-ready items for an ETA with their reasons.
2. queue low (fewer runnable items than min_runnable): submit the program's own declared fillers, then ask the owners
   of not-ready items for ETAs if it is still low.
3. stalemate: route every flag to the item's owner and the dispatcher, with the item's shared policy.

WHY the agent never decides WHAT the work is: items belong to their owners. It prompts, asks, enqueues fillers the
program itself declared, and flags; it never writes job content and never starts, finishes, blocks or edits an item.

Prompts are de-duplicated by key for remind_s; the state lives in `<state_dir>/state.json` so a fresh process
respects it, and every emitted action is appended to `<state_dir>/actions.jsonl`.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robot_activity.metrics import Streak, hours, streaks
from robot_activity.queue import Item, PolicyRef, Queue
from robot_activity.stalemate import assess
from robot_activity.store import Store

if TYPE_CHECKING:
    from robot_activity.notify import Notifier

AGENT_NAME = "robot-activity-agent"
FRESH_S = 60.0          # the latest row must be this recent for "dead now" to mean anything
LOOP_WINDOW_S = 600.0
ETA_ASK_LIMIT = 3       # owners asked per prompt: the top of the not-ready list, not the whole backlog
ACTION_KINDS = ("start_next", "ask_eta", "queue_low", "filled", "stalemate")


@dataclass(frozen=True, kw_only=True)
class Filler:
    """The program's own declared fallback work (recalibrate, self-test, a hard task to play with)."""

    title: str
    owner: str
    est_min: float
    policy: PolicyRef
    robot: str = "any"


@dataclass(frozen=True, kw_only=True)
class AgentConfig:
    idle_after_s: float = 180.0
    min_runnable: int = 1
    remind_s: float = 900.0
    dispatcher: str = "dispatcher"
    fillers: tuple[Filler, ...] = ()
    still_limit_s: float = 300.0


@dataclass(frozen=True, kw_only=True)
class Action:
    kind: str                    # "start_next" | "ask_eta" | "queue_low" | "filled" | "stalemate"
    robot: str
    to: tuple[str, ...]          # recipients, de-duplicated, order kept
    message: str
    item: str | None = None
    policy: str | None = None    # PolicyRef.describe() of the item, when it has one
    key: str = ""                # de-dup key
    asked: tuple[str, ...] = ()  # ask_eta / queue_low: the item ids whose owners are asked


def action_record(action: Action, **extra: Any) -> dict[str, Any]:
    """An Action as a JSON-ready dict (recipients as a list)."""
    return {**extra, **asdict(action), "to": list(action.to)}


def load_config(path: str | Path) -> AgentConfig:
    """Read agent.toml: top-level AgentConfig keys, and [[filler]] tables each with a [filler.policy] table."""
    document = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    filler_tables = document.pop("filler", [])
    known = {spec.name for spec in fields(AgentConfig)} - {"fillers"}
    unknown = set(document) - known
    if unknown:
        raise ValueError(f"{path}: unknown keys {', '.join(sorted(unknown))} (known: {', '.join(sorted(known))})")
    casts = {"idle_after_s": float, "min_runnable": int, "remind_s": float, "dispatcher": str, "still_limit_s": float}
    settings = {key: casts[key](value) for key, value in document.items()}
    return AgentConfig(**settings, fillers=tuple(_filler(table) for table in filler_tables))


def _filler(table: dict[str, Any]) -> Filler:
    policy = table.get("policy")
    if not policy:
        raise ValueError(f"filler {table.get('title')!r} needs a [filler.policy]: fillers run shared code too")
    return Filler(title=table["title"], owner=table["owner"], est_min=float(table["est_min"]),
                  policy=PolicyRef.from_dict(policy), robot=table.get("robot", "any"))


def _unique(names: Iterable[str | None]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(name for name in names if name))


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:.0f} min"


class ActivityAgent:
    def __init__(self, store: Store, queue: Queue, config: AgentConfig = AgentConfig(), notifier: Notifier | None = None,
                 state_dir: str | Path | None = None):
        self.store = store
        self.queue = queue
        self.config = config
        self.notifier = notifier
        self.state_dir = Path(state_dir) if state_dir is not None else store.root / "agent"

    # --- the loop ---------------------------------------------------------------------------------------------------

    def tick(self, now: float) -> list[Action]:
        sent = self._load_state()
        emitted = []
        for robot in self.store.robots():
            rows = self.store.since(robot, now - self._lookback_s(), now)
            # Each rule runs after the previous one's actions are recorded in `sent`, so a later rule sees which
            # items an earlier one already asked about in this same tick.
            for propose in (lambda: self._dead_streak(robot, rows, now, sent), lambda: self._queue_low(robot, now, sent),
                            lambda: self._stalemates(robot, rows, now)):
                for action in propose():
                    last_t = sent.get(action.key)
                    if last_t is not None and now - last_t < self.config.remind_s:
                        continue
                    sent[action.key] = now
                    for item_id in action.asked:
                        sent[_asked_key(item_id)] = now
                    emitted.append(action)
                    self._record(action, now)
        self._save_state(sent, now)
        return emitted

    def _lookback_s(self) -> float:
        """Enough rows to see a dead streak through its first reminder and every stalemate rule."""
        config = self.config
        return max(config.idle_after_s + config.remind_s, config.still_limit_s, LOOP_WINDOW_S) + 2 * FRESH_S

    # --- 1. dead streak ---------------------------------------------------------------------------------------------

    def _dead_streak(self, robot: str, rows: list, now: float, sent: dict[str, float]) -> list[Action]:
        streak = self._trailing_dead(rows, now)
        if streak is None or streak.seconds < self.config.idle_after_s:
            return []
        dead_for = f"{robot} has been dead {_minutes(streak.seconds)} ({_top_reason(streak)})"
        runnable = self.queue.runnable(robot)
        if runnable:
            item = runnable[0]
            policy = item.policy.describe() if item.policy else None
            message = (f"{dead_for}. Next runnable: {item.id} \"{item.title}\" (est {item.est_min:g} min, "
                       f"owner {item.owner}), policy {policy}. Please start it on {robot}.")
            return [Action(kind="start_next", robot=robot, to=(item.executor,), message=message, item=item.id,
                           policy=policy, key=f"start_next:{robot}:{item.id}")]
        asked = self._askable(robot, now, sent)
        if not asked:
            return []
        message = f"{dead_for} and nothing is runnable. ETA please for: {self._reasons(asked)}"
        return [Action(kind="ask_eta", robot=robot, to=_unique(item.owner for item in asked), message=message,
                       key=f"ask_eta:{robot}:{','.join(item.id for item in asked)}",
                       asked=tuple(item.id for item in asked))]

    def _trailing_dead(self, rows: list, now: float) -> Streak | None:
        if not rows or now - rows[-1].t > FRESH_S:
            return None  # WHY: a sampler gap is untracked, never idle - no rows, no prompt
        dead = streaks(rows, "dead")
        if dead and dead[-1].end_t == rows[-1].t:
            return dead[-1]
        return None

    # --- 2. queue low -----------------------------------------------------------------------------------------------

    def _queue_low(self, robot: str, now: float, sent: dict[str, float]) -> list[Action]:
        config = self.config
        if len(self.queue.runnable(robot)) >= config.min_runnable:
            return []
        actions = [self._fill(robot, filler, now) for filler in config.fillers if self._wants(robot, filler)]
        if len(self.queue.runnable(robot)) >= config.min_runnable:
            return actions
        asked = self._askable(robot, now, sent)
        if asked:
            runnable = len(self.queue.runnable(robot))
            message = (f"{robot}'s queue is low ({runnable} runnable, want {config.min_runnable}). "
                       f"ETA please for: {self._reasons(asked)}")
            actions.append(Action(kind="queue_low", robot=robot, to=_unique(item.owner for item in asked),
                                  message=message, key=f"queue_low:{robot}:{','.join(item.id for item in asked)}",
                                  asked=tuple(item.id for item in asked)))
        return actions

    def _wants(self, robot: str, filler: Filler) -> bool:
        if filler.robot not in (robot, "any"):
            return False
        active = self.queue.items(status="queued") + self.queue.items(status="running")
        return not any(item.title == filler.title for item in active)

    def _fill(self, robot: str, filler: Filler, now: float) -> Action:
        # WHY: a filler is the program's own declared work with its own policy; the agent only enqueues it.
        item = self.queue.submit(title=filler.title, owner=filler.owner, est_min=filler.est_min, robot=filler.robot,
                                 source="filler", policy=filler.policy, now=now, by=AGENT_NAME)
        policy = filler.policy.describe()
        message = (f"{robot}'s queue ran low: enqueued filler {item.id} \"{item.title}\" (owner {item.owner}, "
                   f"est {item.est_min:g} min, robot {item.robot}), policy {policy}.")
        return Action(kind="filled", robot=robot, to=(self.config.dispatcher,), message=message, item=item.id,
                      policy=policy, key=f"filled:{robot}:{item.id}")

    # --- 3. stalemate -----------------------------------------------------------------------------------------------

    def _stalemates(self, robot: str, rows: list, now: float) -> list[Action]:
        flags = assess(rows, self.queue.items(), now, still_limit_s=self.config.still_limit_s,
                       loop_window_s=LOOP_WINDOW_S)
        actions = []
        for flag in flags:
            item = self._item_or_none(flag.item)
            policy = item.policy.describe() if item is not None and item.policy else None
            read = (f" Read the policy to judge whether this is expected: {policy}." if policy
                    else " No policy is shared for it.")
            actions.append(Action(kind="stalemate", robot=robot, to=_unique([item.owner if item else None,
                                                                             self.config.dispatcher]),
                                  message=f"{robot} {flag.kind}: {flag.message}.{read}", item=flag.item,
                                  policy=policy, key=f"stalemate:{robot}:{flag.kind}:{flag.item or '-'}"))
        return actions

    def _item_or_none(self, item_id: str | None) -> Item | None:
        if item_id is None:
            return None
        try:
            return self.queue.get(item_id)
        except KeyError:
            return None

    # --- helpers ----------------------------------------------------------------------------------------------------

    def _askable(self, robot: str, now: float, sent: dict[str, float]) -> list[Item]:
        """The not-ready items worth an ETA ask now, in priority order, at most ETA_ASK_LIMIT.

        WHY (2026-11-18): every ask is a turn of the owner's (often a Claude turn, so budget). An owner who gave an
        ETA that has not passed has answered, and an item asked about within remind_s - by either rule - is not
        asked about again. Before this, the demo sent ~75 asks a day to one owner."""
        askable = []
        for item in self.queue.not_ready(robot):
            if item.eta_t is not None and item.eta_t > now:
                continue
            last_t = sent.get(_asked_key(item.id))
            if last_t is not None and now - last_t < self.config.remind_s:
                continue
            askable.append(item)
        return askable[:ETA_ASK_LIMIT]

    def _reasons(self, items: list[Item]) -> str:
        parts = []
        for item in items:
            why = "; ".join(self.queue.why_not_ready(item.id))
            parts.append(f"{item.id} \"{item.title}\" (owner {item.owner}): {why}")
        return " | ".join(parts)

    def _load_state(self) -> dict[str, float]:
        path = self.state_dir / "state.json"
        try:
            return {key: float(value) for key, value in json.loads(path.read_text(encoding="utf-8"))["sent"].items()}
        except (FileNotFoundError, ValueError, KeyError, TypeError, AttributeError):
            return {}

    def _save_state(self, sent: dict[str, float], now: float) -> None:
        keep_s = max(self.config.remind_s, 86400.0)
        current = {key: last_t for key, last_t in sorted(sent.items()) if now - last_t < keep_s}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / "state.json"
        temporary = path.with_name(".state.json.tmp")
        temporary.write_text(json.dumps({"sent": current}, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def _record(self, action: Action, now: float) -> None:
        delivered = self.notifier.deliver(action) if self.notifier is not None else None
        self.state_dir.mkdir(parents=True, exist_ok=True)
        record = action_record(action, t=now)
        if delivered is not None:
            record["delivered"] = delivered
        with (self.state_dir / "actions.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")


def _asked_key(item_id: str) -> str:
    return f"asked:{item_id}"


def _top_reason(streak: Streak) -> str:
    reason, _ = max(streak.reasons.items(), key=lambda entry: entry[1])
    return reason


def agent_kpis(rows: list, actions: list[dict], idle_after_s: float = 180.0, remind_s: float = 900.0) -> dict:
    """The agent's own KPI: seconds a robot sat dead past idle_after_s with no action for it in the preceding remind_s.

    Target 0. `dead_h` is the dead time it is judged against; `prompts` counts the actions for these robots.
    """
    by_robot: dict[str, list] = {}
    for row in rows:
        by_robot.setdefault(row.robot, []).append(row)
    unprompted_s = 0.0
    for robot, robot_rows in by_robot.items():
        covered = _merge([(action["t"], action["t"] + remind_s) for action in actions if action.get("robot") == robot])
        for streak in streaks(robot_rows, "dead"):
            overdue_start = streak.start_t + idle_after_s
            if overdue_start < streak.end_t:
                unprompted_s += (streak.end_t - overdue_start) - _overlap((overdue_start, streak.end_t), covered)
    prompts = sum(1 for action in actions if action.get("robot") in by_robot)
    return {"dead_h": hours(rows).dead_h, "idle_unprompted_s": unprompted_s, "prompts": prompts}


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _overlap(span: tuple[float, float], intervals: list[tuple[float, float]]) -> float:
    return sum(max(0.0, min(span[1], end) - max(span[0], start)) for start, end in intervals)
