# Robot Activity Agent — the contract (v0.1)

The acceptance tests in `tests/` are written from this file, before the implementation. Change this file first,
then the tests, then the code. Owner of the spec: Zach (2026-11-18 daily note, "Robot Activity Agent").

## The remit, in one paragraph

A robot that could be doing useful work and isn't is wasted capital ("sweating the asset", the way a plastics plant
watches machine utilisation). The Robot Activity Agent is a stable sub-assembly with one clear remit: **keep every robot
doing useful work, and say why when it isn't.** It measures activity per robot and per fleet, keeps the work queue full
and moving, and flags stalemates. It is **not** in charge of *what* the work is: queue items belong to their owners
(agents or the deployed system itself). The agent prompts, asks for ETAs, enqueues the program's own declared filler
tasks, and flags; it never writes job content and never starts a job itself.

Goodhart caveat (keep it on the page and in the code): hours moving is a proxy. Some idle time is slack a bottleneck
needs (Theory of Constraints); a throughput KPI (e.g. useful picks per day) is the counterweight. The stalemate rules
are the guard against motion that looks like work.

## Vocabulary (three clean splits of tracked time)

Every tracked second of every robot falls in exactly one side of each split:

| split | sides | rule |
|---|---|---|
| claim | claimed / unclaimed | someone (`claimed_by`) holds the robot |
| activity | activity / dead | claimed AND a queue item is dispatched (`item`) AND `health == "ok"`. Still time inside a running item is activity ("sometimes being still is activity"). Everything else is dead. |
| motion | moving / still / unknown | `moving` is True / False / None (None = no joint feed; never counted as idle) |

Tracked time = the sum of `sample_s` over rows. A gap with no rows (sampler down) is untracked, never idle.

Stock vocabulary this maps onto (docs + landing page, not code): OEE/TEEP availability (activity ÷ tracked is TEEP-style
utilisation, all calendar time), PackML machine states (Execute ≈ activity; Idle/Starved/Held/Aborted ≈ dead reasons).

## Package layout (`src/robot_activity/`)

Runtime dependencies: `robot-msgs>=0.3.0` (PyPI) and the stdlib. `wandb` is an extra (`[wandb]`), imported only inside
`robot_activity.wandb_log`. `import robot_activity` must not import wandb, matplotlib or rerun.

All positions are SI (rad / m). Thresholds are SI inside; degrees only at display.

### `schema.py`

```python
SCHEMA = "robot-activity/1"

@dataclass(frozen=True, kw_only=True)
class QueueSnapshot:
    depth: int                      # queued items for this robot (any status "queued")
    runnable: int                   # of those, ready to start now
    running: str | None = None      # id of the running item, if any
    top_blocker: dict | None = None # {"item", "owner", "kind", "why"} of the highest-priority not-ready item

@dataclass(frozen=True, kw_only=True)
class Sample:                       # one row per robot per window
    t: float                        # window end, unix seconds
    robot: str
    sample_s: float = 10.0
    claimed_by: str | None = None   # owner / agent holding the robot
    item: str | None = None         # queue item id dispatched on the robot
    moving: bool | None = None
    health: str = "ok"              # anything but "ok" (fault, disconnected, estop, ...) is dead
    reason: str | None = None       # explicit why, overrides derived reasons (e.g. "BUS DOWN (rig fault)")
    queue: QueueSnapshot | None = None
    pose: dict[str, float] | None = None   # joint positions at window end (SI), for the looping rule
    def to_dict(self) -> dict: ...  # includes "schema": SCHEMA; None fields may be omitted
    @classmethod
    def from_dict(cls, d: dict) -> "Sample": ...   # round-trips to_dict exactly; ignores unknown keys
```

### `classify.py`

```python
@dataclass(frozen=True)
class Classified:
    claimed: bool
    activity: bool
    motion: str          # "moving" | "still" | "unknown"
    reason: str | None   # None only when activity and moving

def classify(sample: Sample) -> Classified
```

Reason, first match wins:
1. `sample.reason` if set.
2. `health != "ok"` → the health string (`"fault"`, `"disconnected"`, ...).
3. activity (and not moving) → `"in activity (still)"`; activity and moving → `None`; activity and unknown → `None`.
4. dead with a queue snapshot:
   - `queue.runnable > 0` → `f"queue waiting ({n} runnable, not started)"`
   - `queue.depth > 0` and a `top_blocker` → `f"blocked on {kind}: {why}"`
   - `queue.depth == 0` → `"queue empty"`
5. dead, claimed, no item → `"claimed, nothing dispatched"`; dead, unclaimed → `"unclaimed"`.

### `motion.py`

```python
class MotionDetector:
    def __init__(self, threshold: float = math.radians(0.3), window_s: float = 10.0): ...
    def update(self, t: float, joints) -> None   # joints: robot_msgs JointState OR Mapping[str, float]
    def moving(self, now: float) -> bool | None  # any joint's max-min over (now-window_s, now] > threshold;
                                                 # None when no update inside the window
    def pose(self) -> dict[str, float] | None    # latest positions by joint name
```

### `store.py` — files a human can read

```
<root>/samples/<robot>/<YYYY-MM-DD>.jsonl     one Sample.to_dict() per line; the date is t's date in the store's tz
```

```python
class Store:
    def __init__(self, root: str | Path, tz: str | None = None)  # None = system local time
    def append(self, sample: Sample) -> Path
    def rows(self, robot: str, day: str) -> list[Sample]          # sorted by t; skips unparsable lines
    def robots(self) -> list[str]                                  # sorted
    def days(self, robot: str) -> list[str]                        # sorted
    def latest(self, robot: str) -> Sample | None
    def day_of(self, t: float) -> str                              # "YYYY-MM-DD" in tz
```

### `queue.py` — the work queue (one writer API, many readers)

```
<root>/queue/items/<id>.json    one Item each (current state)
<root>/queue/events.jsonl       append-only ledger: {"t","kind","id","robot","by",...}
<root>/queue/.lock              fcntl lock held during every mutation
```

```python
BLOCKER_KINDS = ("code", "dependency", "human", "hardware", "data", "other")
DEFAULT_PRIORITY = {"system": 0, "agent": 5, "filler": 9}   # lower runs first

@dataclass(frozen=True, kw_only=True)
class PolicyRef:            # "share the code or policy you plan to run" — required before an item is ready
    repo: str               # URL or path
    commit: str
    path: str               # file / module inside the repo
    entrypoint: str | None = None
    sha256: str | None = None
    description: str | None = None
    def describe(self) -> str   # one line: "<repo>@<commit[:12]>:<path>[::<entrypoint>]"

@dataclass(frozen=True, kw_only=True)
class Blocker:
    kind: str               # one of BLOCKER_KINDS (else ValueError)
    why: str
    eta_t: float | None = None

@dataclass(frozen=True, kw_only=True)
class Item:
    id: str; title: str; owner: str; executor: str; robot: str   # robot may be "any"
    source: str             # "system" | "agent" | "filler"
    priority: int; est_min: float
    depends_on: tuple[str, ...] = ()
    policy: PolicyRef | None = None
    blockers: tuple[Blocker, ...] = ()
    status: str = "queued"  # queued | running | done | failed | cancelled
    submitted_t: float; started_t: float | None = None; finished_t: float | None = None
    started_by: str | None = None; ran_on: str | None = None; result: str | None = None
    eta_t: float | None = None      # owner's ETA for ready
    note: str | None = None

class QueueError(Exception): ...

class Queue:
    def __init__(self, root: str | Path)
    def submit(self, *, title, owner, est_min, robot="any", executor=None, source="agent", priority=None,
               depends_on=(), policy=None, blockers=(), eta_t=None, note=None, now=None) -> Item
        # est_min > 0 required (QueueError); executor defaults to owner; priority defaults by source
    def get(self, item_id) -> Item                      # KeyError if unknown
    def items(self, *, status=None, robot=None) -> list[Item]   # robot filter matches item.robot == robot or "any"
    def set_policy(self, item_id, policy: PolicyRef, now=None) -> Item
    def block(self, item_id, kind, why, eta_t=None, now=None) -> Item
    def unblock(self, item_id, kind=None, now=None) -> Item   # kind None clears all blockers
    def set_eta(self, item_id, eta_t, now=None) -> Item
    def why_not_ready(self, item_id) -> list[str]
        # [] when ready. Else, in this order: "no policy shared", one "blocked on <kind>: <why>" per blocker,
        # "waiting on dependency <id>" per dependency not finished with result "ok", "status <status>" if not queued
    def is_ready(self, item_id) -> bool
    def runnable(self, robot) -> list[Item]   # ready items for this robot, ordered (priority, submitted_t, id)
    def not_ready(self, robot) -> list[Item]  # queued, not ready, same ordering
    def running(self, robot) -> Item | None
    def start(self, item_id, *, robot, by, now=None) -> Item
        # QueueError (message includes the why_not_ready reasons) if not ready, or if robot already runs an item,
        # or if item.robot is neither robot nor "any"
    def finish(self, item_id, *, result="ok", note=None, now=None) -> Item   # result ok -> status done, else failed
    def cancel(self, item_id, *, note=None, now=None) -> Item
    def snapshot(self, robot) -> QueueSnapshot
    def events(self) -> list[dict]            # the ledger, oldest first (shape frozen below)
```

A second `Queue` on the same root sees every change (state lives in the files, not the object).

Every mutating method also takes an optional keyword `by: str | None = None` (who did it); `submit` defaults it to
the owner and `start` requires it. Other programs that never import this package (e.g. the sister `robot_autonomy`)
write to the queue through the CLI only, never by editing queue files.

#### The events ledger — a frozen, cross-repo seam

`events.jsonl` is read by other repos, so its shape is frozen: one JSON object per line, oldest first. Every event has

| field | type | meaning |
|---|---|---|
| `schema` | `"robot-activity-event/1"` | |
| `t` | float | unix seconds |
| `kind` | str | `submit` · `policy` · `block` · `unblock` · `eta` · `start` · `finish` · `cancel` |
| `id` | str | the item id |
| `robot` | str | the item's robot (`"any"` until it starts); for `start`, `finish`, `cancel` of a started item: the robot it ran on |
| `by` | str or null | who did it |

and, per kind, exactly these extra fields:

| kind | extra fields |
|---|---|
| `submit` | `item`: the full Item as a dict (as in its JSON file) |
| `policy` | `policy`: the PolicyRef as a dict |
| `block` | `blocker`: `{"kind", "why", "eta_t"}` (the Blocker added) |
| `unblock` | `blocker_kind`: the kind cleared, or null when all were cleared; `cleared`: list of the Blocker dicts removed |
| `eta` | `eta_t`: float or null |
| `start` | none (the robot is in `robot`) |
| `finish` | `result`: `"ok"` / anything else (failed); `note`: str or null |
| `cancel` | `note`: str or null |

Sample rows and item files may omit keys whose value is null; readers treat a missing key as null.

### `metrics.py`

```python
@dataclass(frozen=True)
class Hours:
    tracked_h: float
    claimed_h: float; unclaimed_h: float
    activity_h: float; dead_h: float
    moving_h: float; still_h: float; unknown_h: float
    utilization: float      # activity_h / tracked_h (0.0 when nothing tracked)
    moving_share: float     # moving_h / tracked_h

def hours(rows: Iterable[Sample]) -> Hours
    # invariants: claimed+unclaimed == activity+dead == moving+still+unknown == tracked (to 1e-9)

@dataclass(frozen=True)
class ReasonHours:
    reason: str; hours: float; share: float   # share of the total of the table

def why_still(rows) -> list[ReasonHours]  # rows with motion "still", grouped by classify().reason, hours desc
def why_dead(rows) -> list[ReasonHours]   # rows that are dead, grouped by reason, hours desc

@dataclass(frozen=True)
class OwnerHours:
    owner: str; hours: float; moving_h: float; still_h: float; unknown_h: float

def owners(rows) -> list[OwnerHours]       # by claimed_by; unclaimed rows under "unclaimed"; hours desc

@dataclass(frozen=True)
class Streak:
    kind: str; start_t: float; end_t: float; seconds: float; reasons: dict[str, float]  # reason -> seconds

def streaks(rows, kind: str) -> list[Streak]
    # kind "activity" | "dead" | "moving": maximal runs of consecutive rows on that side of the split.
    # Rows are consecutive when t - previous t <= 1.5 * sample_s. start_t = first row's t - sample_s, end_t = last t.

def handoff_gaps(events: list[dict], robot: str) -> list[dict]
    # each finish on robot followed by the next start on robot: {"after", "next", "ended_t", "started_t", "gap_s"}

def minutes(rows) -> list[dict]
    # one dict per wall-clock minute with rows: {"minute_t", "sampled_s", "claimed_frac", "activity_frac",
    #  "moving_frac", "unknown_frac", "queue_depth", "runnable"} (queue fields from the minute's last row, or None)

@dataclass(frozen=True)
class FleetSummary:
    robots: int; robots_active_now: int; robots_moving_now: int
    tracked_h: float; activity_h: float; moving_h: float; utilization: float

def fleet(rows_by_robot: Mapping[str, list[Sample]], now: float, fresh_s: float = 60.0) -> FleetSummary
    # "now" counts use each robot's latest row when now - row.t <= fresh_s
```

### `stalemate.py` — activity that loops in place

```python
@dataclass(frozen=True)
class Flag:
    robot: str; kind: str; message: str; item: str | None; since_t: float

def assess(rows: list[Sample], items: list[Item], now: float, *, still_limit_s=300.0, loop_window_s=600.0,
           pose_grid=math.radians(2.0), overrun_factor=1.5, overrun_slack_s=120.0) -> list[Flag]
```

Rules (each a proxy: a sign to look, never an objective):
- **stuck**: a running item, and the robot `still` for the last `>= still_limit_s` of rows (unknown resets nothing,
  but does not count as still).
- **looping**: within the last `loop_window_s`, the robot moved, came to rest (a still row right after a moving row)
  `>= 3` times at `<= 2` distinct poses (pose rounded to `pose_grid` per joint), and no item finished in the window.
- **overrun**: a running item past `overrun_factor * est_min * 60 + overrun_slack_s` seconds since it started.

### `agent.py` — the agent's one loop

```python
@dataclass(frozen=True, kw_only=True)
class Filler:                    # the program's own declared fallback work (recalibrate, self-test, a hard play task)
    title: str; owner: str; est_min: float; policy: PolicyRef; robot: str = "any"

@dataclass(frozen=True, kw_only=True)
class AgentConfig:
    idle_after_s: float = 180.0
    min_runnable: int = 1
    remind_s: float = 900.0
    dispatcher: str = "dispatcher"
    fillers: tuple[Filler, ...] = ()
    still_limit_s: float = 300.0

def load_config(path) -> AgentConfig      # TOML: top-level keys as above, [[filler]] tables with a [filler.policy]

@dataclass(frozen=True, kw_only=True)
class Action:
    kind: str            # "start_next" | "ask_eta" | "queue_low" | "filled" | "stalemate"
    robot: str
    to: tuple[str, ...]  # recipients (agent names), de-duplicated, order kept
    message: str
    item: str | None = None
    policy: str | None = None     # PolicyRef.describe() of the item, when it has one
    key: str = ""                 # de-dup key

class ActivityAgent:
    def __init__(self, store: Store, queue: Queue, config: AgentConfig = AgentConfig(), notifier=None,
                 state_dir: str | Path | None = None)   # state_dir default <store root>/agent
    def tick(self, now: float) -> list[Action]
```

One `tick`, per robot in `store.robots()`:
1. **dead streak** = the current trailing streak of dead rows (latest row within 60 s of now). If it is
   `>= idle_after_s`:
   - a runnable item exists → `start_next` to `(item.executor,)`, naming the item, its est_min and its policy.
   - else not-ready items exist → `ask_eta` to the owners of up to 3 not-ready items (priority order), each with its
     `why_not_ready` reasons.
2. **queue low**: `len(runnable(robot)) < min_runnable`:
   - for each configured filler for this robot (or "any") with no queued/running item of the same title: submit it
     (source "filler", its policy, ready) and emit `filled` to `(dispatcher,)`.
   - if it is still below `min_runnable` after filling and not-ready items exist: `queue_low` to their owners (ETA ask).
3. **stalemate**: every `stalemate.assess` flag → `stalemate` to `(item owner, dispatcher)` with the item's policy, so
   the reader can check the code to see whether the behaviour is expected.

**ETA asks spend the owner's turns, so ask each item at most once per answer** (added 2026-11-18, after the demo showed
~75 asks/day to one owner): `ask_eta` and `queue_low` only ask about a not-ready item when
- it has no `eta_t`, or its `eta_t` has passed (an owner who gave a future ETA has answered; ask again once it is
  overdue), and
- no `ask_eta` / `queue_low` action asked about it within `remind_s` (whichever rule asked, in this tick or before).

`Action.asked: tuple[str, ...] = ()` lists the item ids whose owners an `ask_eta` / `queue_low` action asks.

De-dup: an action with the same `key` is not emitted again within `remind_s` (state in `<state_dir>/state.json`, so a
fresh agent object respects it). Every emitted action is appended to `<state_dir>/actions.jsonl` (with `t`) and
handed to the notifier. The agent never calls `Queue.start`, `finish`, `block` or `set_policy`.

```python
def agent_kpis(rows: list[Sample], actions: list[dict], idle_after_s=180.0, remind_s=900.0) -> dict
    # {"dead_h", "idle_unprompted_s", "prompts"}: idle_unprompted_s = seconds of dead streaks past idle_after_s with
    # no action for that robot in the preceding remind_s. Target 0: the agent's own KPI.
```

### `notify.py`

```python
class Notifier(Protocol):
    def deliver(self, action: Action) -> bool
class Outbox:            # default: append JSON lines to a file
    def __init__(self, path)
class CommandNotifier:   # run argv with the action's JSON on stdin (Slack webhook script, `claude -p`, ...)
    def __init__(self, argv: list[str], timeout_s: float = 30.0)   # deliver -> True iff exit status 0
```

### `sampler.py`

```python
@dataclass(frozen=True, kw_only=True)
class Observation:          # what a program's probe reports about one robot right now
    claimed_by: str | None = None
    item: str | None = None
    joints: object = None   # robot_msgs JointState or Mapping[str, float]
    health: str = "ok"
    reason: str | None = None

class Sampler:
    def __init__(self, store: Store, robot: str, probe: Callable[[], Observation], queue: Queue | None = None,
                 sample_s: float = 10.0, motion: MotionDetector | None = None)
    def feed(self, t: float, joints) -> None        # optional high-rate joint updates between steps
    def step(self, now: float) -> Sample             # probe, fold joints, write one Sample (queue snapshot if a
                                                     # queue was given; item falls back to queue.running(robot))
```

### `views/`

- `terminal.render_queue(queue, robot=None, now=None) -> str`: the active queue on one screen — what runs now (owner,
  elapsed vs est), the runnable order, then every not-ready item with its WHY, owner and ETA. Plain text, no ANSI by
  default.
- `html.dashboard(store, queue, day, out_path, *, now=None, until=None) -> Path`: one self-contained HTML file (no
  network): fleet now; per robot the three splits as bars; a state ribbon over the day; "Why it stood still"; "Who held
  the robot"; streaks and hand-off gaps; stalemate flags; the active queue; the agent's recent actions. `until` cuts
  every row and event after that time (used to render replay frames).

### `wandb_log.py` (extra `[wandb]`)

```python
def payload(store, queue, robot, day) -> dict
    # pure, no wandb import: {"run_id": f"{robot}-{day}", "steps": minutes(rows), "summary": asdict(hours(rows)),
    #  "tables": {"why_still": [...], "owners": [...], "streaks": [...], "handoff_gaps": [...]}}
def fleet_payload(store, day) -> dict    # {"run_id": f"fleet-{day}", "summary": asdict(fleet(...)), ...}
def push(store, queue, day, project, entity=None) -> dict   # logs the payloads; idempotent (resumes the run id,
                                                             # logs only minutes not yet logged)
```

### `demo.py`

```python
def simulate_fleet(root, *, day="2026-11-18", seed=0, robots=("arm-1", "arm-2", "pendulum-1"), tz="UTC") -> dict
```

A deterministic simulated fleet day (same seed → byte-identical files): 10 s samples for every robot over the day,
queue items and events with owners, policies, blockers of several kinds, one fault episode with an explicit reason,
queue-waiting stretches, filler work, and at least one looping stalemate. Returns `{"rows": n, "items": n, ...}`.

### `cli.py` — entry point `robot-activity`

```
robot-activity demo --root DIR [--day D] [--seed N]
robot-activity summary --root DIR [--day D] [--json]
robot-activity queue [list] --root DIR [--robot R] [--json] [--watch]
robot-activity queue submit --root DIR --title T --owner O --est-min M [--robot R] [--source S] [--priority P]
                            [--policy-repo U --policy-commit C --policy-path P [--policy-entrypoint E]]
robot-activity queue policy|block|unblock|eta|start|finish|cancel ...
robot-activity agent tick|run --root DIR [--config agent.toml] [--notify-cmd "..."] [--every 15]
robot-activity dashboard --root DIR [--day D] --out FILE.html
robot-activity wandb push --root DIR --project P [--entity E] [--day D]
robot-activity sample --root DIR --robot R --probe module:function [--every 10]
```

`--json` output is one JSON document on stdout. Exit 0 on success, 2 on a usage or queue error (message on stderr).

## Drop-in for a coding agent

- `AGENT.md`: the remit as a role prompt any agent harness can load.
- `integrations/claude_code/robot-activity-agent.md`: a Claude Code subagent definition (copy into `.claude/agents/`).

## Worked example on a real robot

`examples/bam_rows.py` maps one lab's existing in-house monitor rows (`bam-robot-activity/1`) to `Sample`s. The parity
test (`tests/test_real_parity.py`, marker `archive`, skipped when the rows are absent) checks this package's numbers
against that monitor's own rollup for one real day, frozen in `tests/data/`.
