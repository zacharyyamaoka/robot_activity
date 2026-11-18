"""A deterministic simulated fleet day: three robots, their owners, the work queue, and the real agent in the loop.

Every 10 s each robot writes one Sample; every minute the real `ActivityAgent` ticks on what has been written, and the
simulated owners and executors react to its prompts (a prompted executor starts the waiting item, an owner asked for an
ETA gives one, an owner told about a stalemate cancels the looping item). Everything goes through the package's own
APIs, so the files on disk are exactly what a real deployment would write. Same seed, byte-identical files.

The day, per robot (times jitter with the seed):

- arm-1, a pick-and-place arm held by pnp-agent 07:00-19:00: batches of picks; a looping regrasp around 10:20 that the
  agent flags; a midday stretch blocked on code (the IK refuses the bin pose) until ik-agent unblocks it; an item
  submitted without a policy, shared later. It declares no filler: on an empty queue the agent has nobody to ask,
  which its own KPI (idle unprompted) makes visible.
- arm-2, a sim-to-real arm held by sim-to-real 08:00-21:00: a nightly system sweep (priority 0), a camera calibration
  that another item depends on, a run that freezes (stuck), a bus fault 14:10-15:20 with an explicit reason, items
  blocked on a human decision and on missing data. Its declared filler (a camera self-check) is started by a person,
  so at night it waits however often the agent prompts.
- pendulum-1, a 1-DOF rack held by rack-agent all day: the agent keeps it busy overnight with its declared filler
  (a swing test), its joint feed drops out for a while (motion unknown, never idle), and one item waits on hardware.
"""

from __future__ import annotations

import bisect
import random
from dataclasses import dataclass, field
from pathlib import Path

from robot_activity.agent import Action, ActivityAgent, AgentConfig, Filler
from robot_activity.queue import Item, PolicyRef, Queue
from robot_activity.schema import Sample
from robot_activity.store import Store

SAMPLE_S = 10.0
AGENT_EVERY_TICKS = 6          # the agent ticks once a minute
HOUR = 3600.0


# --- the day's plan -------------------------------------------------------------------------------------------------

@dataclass
class Plan:
    """One item an owner will submit during the day, and how it will really go."""

    robot: str
    title: str
    owner: str
    at_h: float                            # submit time, hours after midnight
    est_min: float
    run_min: float                         # how long it really runs
    source: str = "agent"
    executor: str | None = None            # None: the owner starts it
    motion: str = "work"                   # "work" | "swing" | "loop" | "freeze"
    policy_at_h: float | None = None       # None: the policy is shared on submit
    blocker: tuple[str, str] | None = None  # (kind, why) blocking it from submit
    unblock_at_h: float | None = None      # None: still blocked at the end of the day
    depends_on: str | None = None          # title of an earlier plan
    outcome: str = "ok"


@dataclass(frozen=True)
class RobotSpec:
    name: str
    holder: str
    shift_h: tuple[float, float]
    joints: tuple[str, ...]


ROBOT_SPECS = {
    "arm-1": RobotSpec("arm-1", "pnp-agent", (7.0, 19.0), ("J1", "J2", "J3", "J4", "J5", "J6")),
    "arm-2": RobotSpec("arm-2", "sim-to-real", (8.0, 21.0), ("J1", "J2", "J3", "J4", "J5", "J6")),
    "pendulum-1": RobotSpec("pendulum-1", "rack-agent", (0.0, 24.0), ("J1",)),
}
FAULT = ("arm-2", 14.17, 15.33, "BUS DOWN (rig fault)")       # robot, from_h, to_h, explicit reason
FEED_OUTAGE = ("pendulum-1", 3.17, 5.67)                      # no joint feed: motion unknown


@dataclass(frozen=True)
class Executor:
    """How quickly someone starts the item at the head of a free robot's queue when nobody prompts them."""

    quick_share: float
    quick_min: tuple[float, float]
    slow_min: tuple[float, float]           # busy elsewhere: often only the agent's prompt gets them going
    awake_h: tuple[float, float] = (0.0, 24.0)


EXECUTORS = {
    "deployment": Executor(1.0, (0.1, 0.5), (0.1, 0.5)),
    "rack-agent": Executor(0.9, (0.2, 1.0), (4.0, 12.0)),
    "pnp-agent": Executor(0.6, (0.3, 2.5), (8.0, 30.0)),
    "sim-to-real": Executor(0.6, (0.3, 2.5), (8.0, 30.0)),
    "rig-owner": Executor(0.3, (1.0, 4.0), (5.0, 20.0), awake_h=(7.0, 19.0)),  # a person: asleep at night
}


def _policy(rng: random.Random, repo: str, path: str, entrypoint: str | None = None) -> PolicyRef:
    return PolicyRef(repo=f"https://github.com/example/{repo}", commit=f"{rng.getrandbits(160):040x}", path=path,
                     entrypoint=entrypoint)


def _plans(rng: random.Random, robots: tuple[str, ...]) -> list[Plan]:
    def jitter(hours: float, spread_min: float = 10.0) -> float:
        return hours + rng.uniform(-spread_min, spread_min) / 60.0

    plans: list[Plan] = []
    if "arm-1" in robots:
        for batch_h in (7.1, 8.4, 9.4, 11.0, 13.5, 14.7, 16.0, 17.3):
            at = jitter(batch_h)
            for index in range(rng.randint(5, 8)):
                title = rng.choice(("pick vial", "pick cap", "pick pill bottle", "place in tray", "pick amber vial"))
                est = rng.choice((6.0, 8.0, 10.0, 12.0))
                plans.append(Plan("arm-1", f"{title} #{len(plans) + 1}", "pnp-agent", at + index * 0.01, est,
                                  est * rng.uniform(0.7, 1.25)))
        plans.append(Plan("arm-1", "regrasp amber vial", "pnp-agent", jitter(10.2, 5), 6.0, 40.0, motion="loop",
                          outcome="cancel"))
        plans.append(Plan("arm-1", "place in bin", "ik-agent", jitter(9.5), 8.0, 9.0, executor="pnp-agent",
                          blocker=("code", "IK refuses the bin pose"), unblock_at_h=jitter(13.3, 5)))
        plans.append(Plan("arm-1", "sweep table", "sweep-agent", jitter(15.0), 12.0, 11.0, policy_at_h=jitter(16.5)))
    if "arm-2" in robots:
        plans.append(Plan("arm-2", "nightly repeatability sweep", "deployment", 0.5, 180.0, rng.uniform(170, 190),
                          source="system"))
        calibrate_at = jitter(8.2)
        plans.append(Plan("arm-2", "calibrate camera", "sim-to-real", calibrate_at, 15.0, 14.0))
        plans.append(Plan("arm-2", "repeatability sweep (calibrated)", "sim-to-real", calibrate_at + 0.05, 25.0, 24.0,
                          depends_on="calibrate camera"))
        for index, at in enumerate((9.2, 10.4, 11.5, 12.6, 13.6, 15.6, 16.8, 18.0, 19.2)):
            est = rng.choice((10.0, 15.0, 20.0))
            plans.append(Plan("arm-2", f"track trajectory {index + 1}", "sim-to-real", jitter(at), est,
                              est * rng.uniform(0.8, 1.2)))
        plans.append(Plan("arm-2", "track fast trajectory", "sim-to-real", jitter(12.0, 5), 10.0, 30.0,
                          motion="freeze", outcome="failed"))
        plans.append(Plan("arm-2", "swap gripper pads", "sim-to-real", jitter(11.0), 20.0, 18.0,
                          blocker=("human", "needs a decision on the pad material"), unblock_at_h=jitter(16.0)))
        plans.append(Plan("arm-2", "train grasp model on caps", "data-agent", jitter(10.0), 30.0, 30.0,
                          blocker=("data", "no labels for the cap yet")))
    if "pendulum-1" in robots:
        for index, at in enumerate((8.5, 12.0, 15.5, 19.0)):
            plans.append(Plan("pendulum-1", f"swing-up trial {index + 1}", "rack-agent", jitter(at), 20.0,
                              rng.uniform(16, 24), motion="swing"))
        plans.append(Plan("pendulum-1", "high-speed swing", "rack-agent", jitter(9.0), 15.0, 14.0, motion="swing",
                          blocker=("hardware", "encoder cable loose"), unblock_at_h=jitter(11.5)))
    return sorted(plans, key=lambda plan: plan.at_h)


def _agent_config(rng: random.Random, robots: tuple[str, ...]) -> AgentConfig:
    """The program's declared fillers. arm-1 declares none, so on an empty queue the agent has nobody to ask."""
    fillers = []
    if "arm-2" in robots:
        fillers.append(Filler(title="camera self-check", owner="rig-owner", est_min=5.0, robot="arm-2",
                              policy=_policy(rng, "rig-tools", "checks/camera_self_check.py", "main")))
    if "pendulum-1" in robots:
        fillers.append(Filler(title="swing test", owner="rack-agent", est_min=8.0, robot="pendulum-1",
                              policy=_policy(rng, "pendulum-rack", "tests/swing_test.py", "main")))
    return AgentConfig(fillers=tuple(fillers))


# --- one robot ------------------------------------------------------------------------------------------------------

@dataclass
class RobotSim:
    spec: RobotSpec
    rng: random.Random
    running: Item | None = None
    plan: Plan | None = None
    finish_t: float = 0.0
    start_at: dict[str, float] = field(default_factory=dict)  # item id -> when its executor will start it
    moving: bool = False
    pose: dict[str, float] = field(default_factory=dict)
    loop_phase: int = 0

    def random_pose(self) -> dict[str, float]:
        return {joint: round(self.rng.uniform(-1.5, 1.5), 4) for joint in self.spec.joints}

    def motion(self, start_t: float, now: float) -> bool:
        """Advance the running item's motion by one sample; returns moving."""
        style = self.plan.motion if self.plan else "work"
        if style == "swing":
            self.pose = self.random_pose()
            return True
        if style == "loop":  # hover, descend, rest at the grasp pose, rise, rest at the hover pose, again
            self.loop_phase = (self.loop_phase + 1) % 12
            if self.loop_phase in (3, 4, 5):
                self.pose = {joint: (0.31 if joint == "J2" else 0.02) for joint in self.spec.joints}
            elif self.loop_phase in (9, 10, 11):
                self.pose = {joint: (0.12 if joint == "J2" else 0.02) for joint in self.spec.joints}
            return self.loop_phase not in (3, 4, 5, 9, 10, 11)
        if style == "freeze" and now - start_t > 0.3 * (self.finish_t - start_t):
            return False  # the run stops making progress and holds still: the stuck rule's case
        stay = 0.88 if self.moving else 0.6  # work: mostly moving, with short pauses inside the job
        moving = self.moving if self.rng.random() < stay else not self.moving
        if moving:
            self.pose = self.random_pose()
        return moving


# --- the fleet ------------------------------------------------------------------------------------------------------

class _RecordingStore(Store):
    """A Store that also keeps what it wrote in memory, so the agent's minute ticks never re-parse the day files."""

    def __init__(self, root: str | Path, tz: str | None):
        super().__init__(root, tz)
        self._written: dict[str, list[Sample]] = {}
        self._times: dict[str, list[float]] = {}

    def append(self, sample: Sample) -> Path:
        self._written.setdefault(sample.robot, []).append(sample)
        self._times.setdefault(sample.robot, []).append(sample.t)
        return super().append(sample)

    def robots(self) -> list[str]:
        return sorted(self._written)

    def since(self, robot: str, since_t: float, until_t: float) -> list[Sample]:
        times = self._times.get(robot, [])
        return self._written[robot][bisect.bisect_right(times, since_t):bisect.bisect_right(times, until_t)] \
            if times else []


class _Fleet:
    def __init__(self, root: Path, day: str, seed: int, robots: tuple[str, ...], tz: str | None):
        self.rng = random.Random(f"robot-activity-demo:{seed}")
        self.store = _RecordingStore(root, tz)
        self.queue = Queue(root)
        self.day_start, self.day_end = self.store.day_bounds(day)
        unknown = [robot for robot in robots if robot not in ROBOT_SPECS]
        if unknown:
            raise ValueError(f"the demo knows robots {', '.join(ROBOT_SPECS)}, not {', '.join(unknown)}")
        self.robots = {robot: RobotSim(ROBOT_SPECS[robot], random.Random(f"{seed}:{robot}")) for robot in robots}
        self.plans = _plans(self.rng, robots)
        self.policies = {plan.title: _policy(self.rng, f"{plan.owner}-policies", f"policies/{_slug(plan.title)}.py")
                         for plan in self.plans}
        self.config = _agent_config(self.rng, robots)
        self.agent = ActivityAgent(self.store, self.queue, self.config)
        self.timeline = _owner_timeline(self.plans)  # (hours, step, plan), in time order
        self.next_step = 0
        self.submitted: dict[str, Item] = {}          # plan title -> item
        self.plan_of: dict[str, Plan] = {}            # item id -> plan
        self.cancel_at: dict[str, float] = {}         # item id -> when its owner reacts to a stalemate
        self.rows = 0

    def at(self, hours: float) -> float:
        return self.day_start + hours * HOUR

    def run(self, until: float | None = None) -> dict:
        # WHY until: a demo of today that ran to midnight left items "running" from hours that had not happened yet.
        end = self.day_end if until is None else min(self.day_end, until)
        tick = 0
        now = self.day_start
        while now <= end and now < self.day_end:
            self._owners_act(now)
            for robot in self.robots.values():
                self._step(robot, now)
            if tick % AGENT_EVERY_TICKS == 0:
                self._react(self.agent.tick(now), now)
            tick += 1
            now = self.day_start + tick * SAMPLE_S
        return {"rows": self.rows, "items": len(self.queue.items()), "events": len(self.queue.events())}

    # owners: submit, share policies, unblock on their own schedule ----------------------------------------------------

    def _owners_act(self, now: float) -> None:
        while self.next_step < len(self.timeline) and self.at(self.timeline[self.next_step][0]) <= now:
            _, step, plan = self.timeline[self.next_step]
            self.next_step += 1
            if step == "submit":
                self._submit(plan, now)
            elif step == "share policy":
                self.queue.set_policy(self.submitted[plan.title].id, self.policies[plan.title], now=now, by=plan.owner)
            elif step == "unblock":
                self.queue.unblock(self.submitted[plan.title].id, now=now, by=plan.owner)

    def _submit(self, plan: Plan, now: float) -> None:
        depends_on = (self.submitted[plan.depends_on].id,) if plan.depends_on in self.submitted else ()
        item = self.queue.submit(title=plan.title, owner=plan.owner, executor=plan.executor, est_min=plan.est_min,
                                 robot=plan.robot, source=plan.source, depends_on=depends_on,
                                 policy=None if plan.policy_at_h is not None else self.policies[plan.title], now=now)
        if plan.blocker:  # the owner says why it cannot run yet: one more line in the ledger
            item = self.queue.block(item.id, plan.blocker[0], plan.blocker[1], now=now, by=plan.owner)
        self.submitted[plan.title] = item
        self.plan_of[item.id] = plan

    # robots: finish, fault, start, move, sample ---------------------------------------------------------------------

    def _step(self, robot: RobotSim, now: float) -> None:
        name = robot.spec.name
        faulted = name == FAULT[0] and self.at(FAULT[1]) <= now < self.at(FAULT[2])
        if robot.running is not None:
            self._maybe_end(robot, now, faulted)
        if robot.running is None and not faulted:
            self._maybe_start(robot, now)
        if robot.running is not None:
            robot.moving = robot.motion(robot.running.started_t or now, now)
        else:
            robot.moving = False
        claimed_by = robot.running.executor if robot.running else self._holder(robot, now)
        feed_down = name == FEED_OUTAGE[0] and self.at(FEED_OUTAGE[1]) <= now < self.at(FEED_OUTAGE[2])
        sample = Sample(t=now, robot=name, sample_s=SAMPLE_S, claimed_by=claimed_by,
                        item=robot.running.id if robot.running else None,
                        moving=None if feed_down else robot.moving,
                        health="fault" if faulted else "ok", reason=FAULT[3] if faulted else None,
                        queue=self.queue.snapshot(name), pose=None if feed_down else dict(robot.pose or {}) or None)
        self.store.append(sample)
        self.rows += 1

    def _holder(self, robot: RobotSim, now: float) -> str | None:
        start_h, end_h = robot.spec.shift_h
        return robot.spec.holder if self.at(start_h) <= now < self.at(end_h) else None

    def _maybe_end(self, robot: RobotSim, now: float, faulted: bool) -> None:
        item = robot.running
        if faulted:
            self.queue.finish(item.id, result="fault", note="the bus went down mid-run", now=now, by=item.executor)
        elif item.id in self.cancel_at and now >= self.cancel_at[item.id]:
            plan = self.plan_of[item.id]
            if plan.outcome == "cancel":
                self.queue.cancel(item.id, note="regrasp loop: cancelled to fix the perception", now=now,
                                  by=item.owner)
            else:
                self.queue.finish(item.id, result=plan.outcome, note="stopped after the stalemate flag", now=now,
                                  by=item.owner)
        elif now >= robot.finish_t and robot.plan is not None and robot.plan.motion not in ("loop", "freeze"):
            self.queue.finish(item.id, result=robot.plan.outcome, now=now, by=item.executor)
        else:
            return
        robot.running, robot.plan = None, None

    def _maybe_start(self, robot: RobotSim, now: float) -> None:
        runnable = self.queue.runnable(robot.spec.name)
        if not runnable:
            return
        head = runnable[0]
        executor = EXECUTORS.get(head.executor, EXECUTORS["pnp-agent"])
        if not self._awake(executor, now):
            return
        if head.id not in robot.start_at:
            quick = robot.rng.random() < executor.quick_share
            delay_min = robot.rng.uniform(*(executor.quick_min if quick else executor.slow_min))
            robot.start_at[head.id] = now + delay_min * 60.0
        if now < robot.start_at[head.id]:
            return
        robot.running = self.queue.start(head.id, robot=robot.spec.name, by=head.executor, now=now)
        robot.plan = self.plan_of.get(head.id) or self._filler_plan(head)
        robot.finish_t = now + robot.plan.run_min * 60.0
        robot.loop_phase = 0

    def _awake(self, executor: Executor, now: float) -> bool:
        return self.at(executor.awake_h[0]) <= now < self.at(executor.awake_h[1])

    def _filler_plan(self, item: Item) -> Plan:
        motion = "swing" if item.robot == "pendulum-1" else "work"
        plan = Plan(item.robot, item.title, item.owner, 0.0, item.est_min, item.est_min * self.rng.uniform(0.85, 1.1),
                    source="filler", motion=motion)
        self.plan_of[item.id] = plan
        return plan

    # reactions to the agent's actions ---------------------------------------------------------------------------------

    def _react(self, actions: list[Action], now: float) -> None:
        for action in actions:
            robot = self.robots.get(action.robot)
            if action.kind == "start_next" and robot is not None:
                executor = EXECUTORS.get(action.to[0], EXECUTORS["pnp-agent"])
                if self._awake(executor, now) and robot.rng.random() < 0.85:  # a prompt usually gets it started
                    prompt_start = now + robot.rng.uniform(20.0, 90.0)
                    robot.start_at[action.item] = min(robot.start_at.get(action.item, prompt_start), prompt_start)
            elif action.kind in ("ask_eta", "queue_low"):
                self._answer_eta(action, now)
            elif action.kind == "stalemate" and action.item and action.item not in self.cancel_at:
                self.cancel_at[action.item] = now + self.rng.uniform(2.0, 5.0) * 60.0

    def _answer_eta(self, action: Action, now: float) -> None:
        for item in self.queue.not_ready(action.robot):
            plan = self.plan_of.get(item.id)
            if plan is None or item.owner not in action.to or item.eta_t is not None:
                continue
            ready_h = plan.unblock_at_h or plan.policy_at_h
            eta = self.at(ready_h) + self.rng.uniform(-10, 20) * 60.0 if ready_h else self.day_end + 9 * HOUR
            self.queue.set_eta(item.id, round(eta), now=now, by=item.owner)


def _owner_timeline(plans: list[Plan]) -> list[tuple[float, str, Plan]]:
    """What the owners do on their own schedule: submit, later share a policy, later clear a blocker."""
    steps = [(plan.at_h, "submit", plan) for plan in plans]
    steps += [(plan.policy_at_h, "share policy", plan) for plan in plans if plan.policy_at_h is not None]
    steps += [(plan.unblock_at_h, "unblock", plan) for plan in plans if plan.unblock_at_h is not None]
    order = {"submit": 0, "share policy": 1, "unblock": 2}
    return sorted(steps, key=lambda step: (step[0], order[step[1]]))


def _slug(title: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in title.lower()).strip("_")


def simulate_fleet(root: str | Path, *, day: str = "2026-11-18", seed: int = 0,
                   robots: tuple[str, ...] = ("arm-1", "arm-2", "pendulum-1"), tz: str | None = "UTC",
                   until: float | None = None) -> dict:
    """Write one simulated fleet day under `root` (samples, queue, agent state) and return what was written.

    `until` stops the day there: nothing is written after it, so the queue on disk is the queue as it stood then."""
    fleet = _Fleet(Path(root), day, seed, tuple(robots), tz)
    counts = fleet.run(until)
    return {"root": str(root), "day": day, "seed": seed, "tz": tz, "robots": list(robots), **counts,
            "actions": _count_lines(Path(root) / "agent" / "actions.jsonl")}


def _count_lines(path: Path) -> int:
    return sum(1 for _ in path.open(encoding="utf-8")) if path.is_file() else 0

