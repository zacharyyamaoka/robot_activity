"""The work queue: one writer API, many readers, and the files are the state.

```
<root>/queue/items/<id>.json    one Item each (current state, rewritten atomically)
<root>/queue/events.jsonl       append-only ledger, shape frozen as "robot-activity-event/1" (other repos read it)
<root>/queue/.lock              fcntl lock held during every mutation
```

Items belong to their owners. An item is ready only when its owner has shared the code or policy it will run
(`PolicyRef`), nothing blocks it and its dependencies finished ok. Lower priority runs first: a deployed system's own
work (0), then agent work (5), then the program's declared fillers (9).

Every mutation appends exactly one event. The ledger only grows, so its size is the queue's version: a reader keeps the
parsed items (and the views derived from them) until it changes, then re-reads only the item files that changed. That
keeps a 10 s sampler and a minute agent tick cheap on a queue with a long history.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from robot_activity.schema import QueueSnapshot
from robot_activity.store import check_name

EVENT_SCHEMA = "robot-activity-event/1"
BLOCKER_KINDS = ("code", "dependency", "human", "hardware", "data", "other")
DEFAULT_PRIORITY = {"system": 0, "agent": 5, "filler": 9}   # lower runs first
STATUSES = ("queued", "running", "done", "failed", "cancelled")
ANY_ROBOT = "any"


class QueueError(Exception):
    """A queue rule refused the change (not ready, robot busy, wrong robot, bad estimate, ...)."""


@dataclass(frozen=True, kw_only=True)
class PolicyRef:
    """The code or policy an item will run: "share the code you plan to run" before it may start."""

    repo: str                       # URL or path
    commit: str
    path: str                       # file / module inside the repo
    entrypoint: str | None = None
    sha256: str | None = None
    description: str | None = None

    def describe(self) -> str:
        text = f"{self.repo}@{self.commit[:12]}:{self.path}"
        return f"{text}::{self.entrypoint}" if self.entrypoint else text

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> PolicyRef:
        return cls(repo=record["repo"], commit=record["commit"], path=record["path"],
                   entrypoint=record.get("entrypoint"), sha256=record.get("sha256"),
                   description=record.get("description"))


@dataclass(frozen=True, kw_only=True)
class Blocker:
    """Why an item cannot run yet, in one of a few kinds, with the owner's ETA when known."""

    kind: str
    why: str
    eta_t: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in BLOCKER_KINDS:
            raise ValueError(f"blocker kind {self.kind!r} is not one of {', '.join(BLOCKER_KINDS)}")

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> Blocker:
        return cls(kind=record["kind"], why=record["why"], eta_t=record.get("eta_t"))


@dataclass(frozen=True, kw_only=True)
class Item:
    id: str
    title: str
    owner: str                      # who the item belongs to (decides WHAT it is)
    executor: str                   # who starts it
    robot: str                      # a robot name, or "any"
    source: str                     # "system" | "agent" | "filler"
    priority: int
    est_min: float
    depends_on: tuple[str, ...] = ()
    policy: PolicyRef | None = None
    blockers: tuple[Blocker, ...] = ()
    status: str = "queued"          # queued | running | done | failed | cancelled
    submitted_t: float
    started_t: float | None = None
    finished_t: float | None = None
    started_by: str | None = None
    ran_on: str | None = None
    result: str | None = None
    eta_t: float | None = None      # owner's ETA for ready
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        record = asdict(self)
        record["depends_on"] = list(self.depends_on)
        record["blockers"] = [asdict(blocker) for blocker in self.blockers]
        return record

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> Item:
        policy = record.get("policy")
        return cls(
            id=record["id"], title=record["title"], owner=record["owner"],
            executor=record.get("executor") or record["owner"], robot=record.get("robot") or ANY_ROBOT,
            source=record.get("source") or "agent", priority=int(record["priority"]), est_min=float(record["est_min"]),
            depends_on=tuple(record.get("depends_on") or ()),
            policy=PolicyRef.from_dict(policy) if policy else None,
            blockers=tuple(Blocker.from_dict(blocker) for blocker in record.get("blockers") or ()),
            status=record.get("status") or "queued", submitted_t=float(record["submitted_t"]),
            started_t=record.get("started_t"), finished_t=record.get("finished_t"),
            started_by=record.get("started_by"), ran_on=record.get("ran_on"), result=record.get("result"),
            eta_t=record.get("eta_t"), note=record.get("note"),
        )

    @property
    def order(self) -> tuple[int, float, str]:
        """Start order: priority, then first submitted, then id."""
        return (self.priority, self.submitted_t, self.id)

    def fits(self, robot: str) -> bool:
        return self.robot in (robot, ANY_ROBOT)


@dataclass
class _Cache:
    version: tuple[int, int] | None = None   # (ledger inode, ledger size): the ledger only grows
    items: dict[str, Item] = field(default_factory=dict)
    stamps: dict[str, tuple[int, int, int]] = field(default_factory=dict)   # file name -> (inode, mtime_ns, size)
    views: dict[tuple[str, str], Any] = field(default_factory=dict)         # derived views, valid for this version


class Queue:
    """The queue under `<root>/queue`. Any number of Queue objects (and processes) may share one root."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.folder = self.root / "queue"
        self.items_dir = self.folder / "items"
        self.ledger = self.folder / "events.jsonl"
        self.lock_path = self.folder / ".lock"
        self._cache = _Cache()

    # --- read -------------------------------------------------------------------------------------------------------

    def get(self, item_id: str) -> Item:
        try:
            return self._load()[item_id]
        except KeyError:
            raise KeyError(item_id) from None

    def items(self, *, status: str | None = None, robot: str | None = None) -> list[Item]:
        selected = [item for item in self._load().values()
                    if (status is None or item.status == status) and (robot is None or item.fits(robot))]
        return sorted(selected, key=lambda item: (item.submitted_t, item.id))

    def why_not_ready(self, item_id: str) -> list[str]:
        items = self._load()
        if item_id not in items:
            raise KeyError(item_id)
        return _why_not_ready(items[item_id], items)

    def is_ready(self, item_id: str) -> bool:
        return not self.why_not_ready(item_id)

    def runnable(self, robot: str) -> list[Item]:
        return list(self._view("runnable", robot, lambda items: self._split(robot, items)[0]))

    def not_ready(self, robot: str) -> list[Item]:
        return list(self._view("not_ready", robot, lambda items: self._split(robot, items)[1]))

    def running(self, robot: str) -> Item | None:
        return self._view("running", robot, lambda items: next(
            (item for item in items.values() if item.status == "running" and item.ran_on == robot), None))

    def snapshot(self, robot: str) -> QueueSnapshot:
        return self._view("snapshot", robot, lambda items: self._snapshot(robot, items))

    def events(self) -> list[dict]:
        if not self.ledger.is_file():
            return []
        events = []
        for line in self.ledger.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events

    # --- write ------------------------------------------------------------------------------------------------------

    def submit(self, *, title: str, owner: str, est_min: float, robot: str = ANY_ROBOT, executor: str | None = None,
               source: str = "agent", priority: int | None = None, depends_on: Iterable[str] = (),
               policy: PolicyRef | None = None, blockers: Iterable[Blocker] = (), eta_t: float | None = None,
               note: str | None = None, now: float | None = None, by: str | None = None) -> Item:
        if not title or not owner:
            raise QueueError("an item needs a title and an owner")
        if not est_min > 0:
            raise QueueError(f"est_min must be > 0 (got {est_min!r}): an owner's estimate is what overrun is judged by")
        if source not in DEFAULT_PRIORITY and priority is None:
            raise QueueError(f"unknown source {source!r}: one of {', '.join(DEFAULT_PRIORITY)}, or pass a priority")
        check_name(robot, "robot")
        blockers = tuple(blockers)
        if not all(isinstance(blocker, Blocker) for blocker in blockers):
            raise QueueError("blockers must be Blocker values")
        now = _now(now)
        with self._locked():
            item = Item(id=self._new_id(title), title=title, owner=owner, executor=executor or owner, robot=robot,
                        source=source, priority=DEFAULT_PRIORITY[source] if priority is None else int(priority),
                        est_min=float(est_min), depends_on=tuple(depends_on), policy=policy, blockers=blockers,
                        submitted_t=now, eta_t=eta_t, note=note)
            self._write(item)
            self._append("submit", item, now, owner if by is None else by, item=item.to_dict())
        return item

    def set_policy(self, item_id: str, policy: PolicyRef, now: float | None = None, by: str | None = None) -> Item:
        now = _now(now)
        with self._locked():
            item = self._editable(item_id, statuses=("queued",))
            item = self._write(replace(item, policy=policy))
            self._append("policy", item, now, by, policy=asdict(policy))
        return item

    def block(self, item_id: str, kind: str, why: str, eta_t: float | None = None, now: float | None = None,
              by: str | None = None) -> Item:
        blocker = Blocker(kind=kind, why=why, eta_t=eta_t)  # ValueError on an unknown kind, before anything is written
        now = _now(now)
        with self._locked():
            item = self._editable(item_id)
            item = self._write(replace(item, blockers=(*item.blockers, blocker)))
            self._append("block", item, now, by, blocker=asdict(blocker))
        return item

    def unblock(self, item_id: str, kind: str | None = None, now: float | None = None, by: str | None = None) -> Item:
        if kind is not None and kind not in BLOCKER_KINDS:
            raise ValueError(f"blocker kind {kind!r} is not one of {', '.join(BLOCKER_KINDS)}")
        now = _now(now)
        with self._locked():
            item = self._editable(item_id)
            cleared = [blocker for blocker in item.blockers if kind is None or blocker.kind == kind]
            kept = tuple(blocker for blocker in item.blockers if blocker not in cleared)
            item = self._write(replace(item, blockers=kept))
            self._append("unblock", item, now, by, blocker_kind=kind,
                         cleared=[asdict(blocker) for blocker in cleared])
        return item

    def set_eta(self, item_id: str, eta_t: float | None, now: float | None = None, by: str | None = None) -> Item:
        now = _now(now)
        with self._locked():
            item = self._write(replace(self._editable(item_id), eta_t=eta_t))
            self._append("eta", item, now, by, eta_t=eta_t)
        return item

    def start(self, item_id: str, *, robot: str, by: str, now: float | None = None) -> Item:
        check_name(robot, "robot")
        if robot == ANY_ROBOT:
            raise QueueError('start needs the robot the item runs on, not "any"')
        now = _now(now)
        with self._locked():
            items = self._load()
            item = self._editable(item_id, statuses=STATUSES, items=items)  # a wrong status is named among the reasons
            if not item.fits(robot):
                raise QueueError(f"{item_id} is for robot {item.robot!r}, not {robot!r}")
            # WHY: an item is ready only once its owner shared the code it will run (PolicyRef), so whoever sees a
            # stalemate can read that code and judge whether the behaviour is expected.
            reasons = _why_not_ready(item, items)
            if reasons:
                raise QueueError(f"{item_id} is not ready: {'; '.join(reasons)}")
            busy = self.running(robot)
            if busy is not None:
                raise QueueError(f"{robot} already runs {busy.id} ({busy.title}); one running item per robot")
            item = self._write(replace(item, status="running", started_t=now, started_by=by, ran_on=robot))
            self._append("start", item, now, by)
        return item

    def finish(self, item_id: str, *, result: str = "ok", note: str | None = None, now: float | None = None,
               by: str | None = None) -> Item:
        now = _now(now)
        with self._locked():
            item = self._editable(item_id, statuses=("running",))
            item = self._write(replace(item, status="done" if result == "ok" else "failed", result=result,
                                       finished_t=now, note=note if note is not None else item.note))
            self._append("finish", item, now, by, result=result, note=note)
        return item

    def cancel(self, item_id: str, *, note: str | None = None, now: float | None = None, by: str | None = None) -> Item:
        now = _now(now)
        with self._locked():
            item = self._editable(item_id, statuses=("queued", "running"))
            item = self._write(replace(item, status="cancelled", finished_t=now,
                                       note=note if note is not None else item.note))
            self._append("cancel", item, now, by, note=note)
        return item

    # --- internals --------------------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.items_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _version(self) -> tuple[int, int] | None:
        try:
            status = self.ledger.stat()
        except FileNotFoundError:
            return None
        return (status.st_ino, status.st_size)

    def _load(self) -> dict[str, Item]:
        """Every item by id. Nothing is re-read until the ledger grows; then only the item files that changed."""
        version = self._version()
        if version is not None and version == self._cache.version:
            return self._cache.items
        previous = self._cache
        items: dict[str, Item] = {}
        stamps: dict[str, tuple[int, int, int]] = {}
        if self.items_dir.is_dir():
            with os.scandir(self.items_dir) as entries:
                for entry in entries:
                    if not entry.name.endswith(".json") or entry.name.startswith("."):
                        continue
                    status = entry.stat()
                    stamp = (status.st_ino, status.st_mtime_ns, status.st_size)
                    item = previous.items.get(entry.name[:-5]) if previous.stamps.get(entry.name) == stamp else None
                    if item is None:
                        item = _read_item(Path(entry.path))
                    if item is not None:
                        items[item.id] = item
                        stamps[entry.name] = stamp
        self._cache = _Cache(version=version, items=items, stamps=stamps)
        return items

    def _view(self, name: str, robot: str, compute: Callable[[dict[str, Item]], Any]) -> Any:
        """A derived view (runnable order, snapshot, ...) computed once per ledger version and robot."""
        items = self._load()
        key = (name, robot)
        if key not in self._cache.views:
            self._cache.views[key] = compute(items)
        return self._cache.views[key]

    def _split(self, robot: str, items: dict[str, Item]) -> tuple[list[Item], list[Item]]:
        """This robot's queued items in start order, split into (ready, not ready)."""
        ready, waiting = [], []
        for item in sorted(self._queued_for(robot, items), key=lambda item: item.order):
            (waiting if _why_not_ready(item, items) else ready).append(item)
        return ready, waiting

    def _editable(self, item_id: str, statuses: tuple[str, ...] = ("queued", "running"),
                  items: dict[str, Item] | None = None) -> Item:
        items = self._load() if items is None else items
        if item_id not in items:
            raise KeyError(item_id)
        item = items[item_id]
        if item.status not in statuses:
            raise QueueError(f"{item_id} is {item.status}; this change needs status {' or '.join(statuses)}")
        return item

    def _new_id(self, title: str) -> str:
        """`<sequence>-<slug of the title>`: unique under the lock, sorted by submission in `ls`, readable."""
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:32].strip("-") or "item"
        number = sum(1 for _ in self.items_dir.glob("*.json")) + 1
        while (self.items_dir / f"{number:04d}-{slug}.json").exists():
            number += 1
        return f"{number:04d}-{slug}"

    def _write(self, item: Item) -> Item:
        path = self.items_dir / f"{check_name(item.id, 'item id')}.json"
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(item.to_dict(), indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)  # readers never see half an item
        return item

    def _append(self, kind: str, subject: Item, now: float, by: str | None, **extra: Any) -> None:
        event = {"schema": EVENT_SCHEMA, "t": float(now), "kind": kind, "id": subject.id,
                 "robot": subject.ran_on or subject.robot, "by": by, **extra}
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")

    @staticmethod
    def _queued_for(robot: str, items: dict[str, Item]) -> Iterator[Item]:
        return (item for item in items.values() if item.status == "queued" and item.fits(robot))

    def _snapshot(self, robot: str, items: dict[str, Item]) -> QueueSnapshot:
        ready, waiting = self._split(robot, items)
        running = self.running(robot)
        top_blocker = None
        if waiting:
            top = waiting[0]
            kind, why = _top_reason(top, items)
            top_blocker = {"item": top.id, "owner": top.owner, "kind": kind, "why": why}
        return QueueSnapshot(depth=len(ready) + len(waiting), runnable=len(ready),
                             running=running.id if running else None, top_blocker=top_blocker)


def _read_item(path: Path) -> Item | None:
    try:
        return Item.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _now(now: float | None) -> float:
    return float(time.time() if now is None else now)


def _why_not_ready(item: Item, items: dict[str, Item]) -> list[str]:
    reasons = []
    if item.policy is None:
        reasons.append("no policy shared")
    reasons.extend(f"blocked on {blocker.kind}: {blocker.why}" for blocker in item.blockers)
    for dependency in item.depends_on:
        upstream = items.get(dependency)
        if upstream is None or upstream.status != "done" or upstream.result != "ok":
            reasons.append(f"waiting on dependency {dependency}")
    if item.status != "queued":
        reasons.append(f"status {item.status}")
    return reasons


def _top_reason(item: Item, items: dict[str, Item]) -> tuple[str, str]:
    """The (kind, why) a dead robot reports for its top not-ready item: the owner's own blocker first."""
    if item.blockers:
        return item.blockers[0].kind, item.blockers[0].why
    if item.policy is None:
        return "code", "no policy shared"
    for reason in _why_not_ready(item, items):
        if reason.startswith("waiting on dependency"):
            return "dependency", reason
    return "other", "; ".join(_why_not_ready(item, items))


def reasons_not_ready(item: Item, items: dict[str, Item]) -> list[str]:
    """`Queue.why_not_ready` over a given set of items (e.g. a replayed past state)."""
    return _why_not_ready(item, items)


class Replay:
    """The queue as it stood at any time, rebuilt from the events ledger alone (the ledger is the full history).

    `at(t)` applies every event up to t; call it with non-decreasing times to walk a day forward cheaply.
    """

    def __init__(self, events: Iterable[dict]):
        self._events = sorted(events, key=lambda event: event["t"])
        self._next = 0
        self.items: dict[str, Item] = {}

    def at(self, t: float) -> dict[str, Item]:
        while self._next < len(self._events) and self._events[self._next]["t"] <= t:
            self._apply(self._events[self._next])
            self._next += 1
        return self.items

    def _apply(self, event: dict) -> None:
        kind, item_id, t = event["kind"], event["id"], float(event["t"])
        if kind == "submit":
            self.items[item_id] = Item.from_dict(event["item"])
            return
        item = self.items.get(item_id)
        if item is None:
            return
        if kind == "policy":
            item = replace(item, policy=PolicyRef.from_dict(event["policy"]))
        elif kind == "block":
            item = replace(item, blockers=(*item.blockers, Blocker.from_dict(event["blocker"])))
        elif kind == "unblock":
            cleared = event.get("blocker_kind")
            item = replace(item, blockers=tuple(blocker for blocker in item.blockers
                                                if cleared is not None and blocker.kind != cleared))
        elif kind == "eta":
            item = replace(item, eta_t=event.get("eta_t"))
        elif kind == "start":
            item = replace(item, status="running", started_t=t, started_by=event.get("by"), ran_on=event["robot"])
        elif kind == "finish":
            result = event.get("result") or "ok"
            item = replace(item, status="done" if result == "ok" else "failed", result=result, finished_t=t,
                           note=event.get("note") if event.get("note") is not None else item.note)
        elif kind == "cancel":
            item = replace(item, status="cancelled", finished_t=t,
                           note=event.get("note") if event.get("note") is not None else item.note)
        self.items[item_id] = item


def replay(events: Iterable[dict], until: float | None = None) -> dict[str, Item]:
    """Every item as it stood at `until` (default: the end of the ledger)."""
    return Replay(events).at(math.inf if until is None else until)
