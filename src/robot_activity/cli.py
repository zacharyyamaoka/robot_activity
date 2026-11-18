"""`robot-activity`: the command line over the store, the queue, the agent and the views.

Programs that never import this package write to the queue through these commands only, never by editing the files.
`--json` prints one JSON document on stdout. Exit status 0 on success, 2 on a usage or queue error (message on stderr).
`--root` defaults to $ROBOT_ACTIVITY_ROOT; `--tz` (the store's time zone for day files) to $ROBOT_ACTIVITY_TZ, else the
system's local time.
"""

from __future__ import annotations

import argparse
import datetime
import importlib
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from robot_activity.queue import Blocker, PolicyRef, Queue, QueueError
from robot_activity.store import Store

QUEUE_COMMANDS = ("list", "submit", "policy", "block", "unblock", "eta", "start", "finish", "cancel")


class UsageError(Exception):
    """A command line that cannot run: exit 2 with the message."""


# --- shared ---------------------------------------------------------------------------------------------------------

def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=os.environ.get("ROBOT_ACTIVITY_ROOT"),
                        help="the data folder (default: $ROBOT_ACTIVITY_ROOT)")
    parser.add_argument("--tz", default=os.environ.get("ROBOT_ACTIVITY_TZ"),
                        help="time zone of the day files, e.g. UTC or America/Vancouver (default: system local)")


def _root(args: argparse.Namespace) -> Path:
    if not args.root:
        raise UsageError("--root is required (or set ROBOT_ACTIVITY_ROOT)")
    return Path(args.root)


def _store(args: argparse.Namespace) -> Store:
    return Store(_root(args), tz=args.tz)


def _day(args: argparse.Namespace, store: Store) -> str:
    return args.day or store.day_of(time.time())


def _print_json(document: Any) -> None:
    print(json.dumps(document, indent=2, default=str))


def parse_time(text: str | None, now: float | None = None) -> float | None:
    """Unix seconds, an ISO 8601 time ("2026-11-18T14:30", local unless it has an offset), "+90m" / "+2h", or "none"."""
    if text is None or text.lower() == "none":
        return None
    now = time.time() if now is None else now
    if text.startswith("+"):
        units = {"s": 1.0, "m": 60.0, "h": 3600.0}
        unit = text[-1].lower()
        if unit not in units:
            raise UsageError(f"relative time {text!r} needs a unit: s, m or h (e.g. +90m)")
        return now + float(text[1:-1]) * units[unit]
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        raise UsageError(f"cannot read the time {text!r}: use unix seconds, ISO 8601 or +90m") from None


# --- commands -------------------------------------------------------------------------------------------------------

def cmd_demo(args: argparse.Namespace) -> int:
    from robot_activity.demo import simulate_fleet

    # WHY today by default: then summary, queue, agent tick and dashboard (all "today" by default) chain without --day.
    # WHY until now for today: the simulated day stops at the present, so nothing on disk is in the future.
    store = _store(args)
    day = _day(args, store)
    now = time.time()
    until = now if day == store.day_of(now) else None
    result = simulate_fleet(_root(args), day=day, seed=args.seed, tz=args.tz, until=until)
    if args.json:
        _print_json(result)
    else:
        print(f"simulated {result['day']} for {', '.join(result['robots'])}: {result['rows']} rows, "
              f"{result['items']} items, {result['actions']} agent actions -> {result['root']}")
        print(f"next: robot-activity summary --root {result['root']} --day {result['day']}"
              + (f" --tz {args.tz}" if args.tz else ""))
    return 0


def summary_document(store: Store, queue: Queue, day: str, now: float | None = None) -> dict[str, Any]:
    from robot_activity.agent import agent_kpis
    from robot_activity.metrics import fleet, hours, owners, why_dead, why_still

    rows_by_robot = {robot: store.rows(robot, day) for robot in store.robots()}
    rows_by_robot = {robot: rows for robot, rows in rows_by_robot.items() if rows}
    day_end = store.day_bounds(day)[1]
    now = min(time.time(), day_end) if now is None else now
    actions = _actions(store.root)
    robots = {}
    for robot, rows in rows_by_robot.items():
        running = queue.running(robot)
        robots[robot] = {
            "hours": asdict(hours(rows)),
            "why_dead": [asdict(entry) for entry in why_dead(rows)[:5]],
            "why_still": [asdict(entry) for entry in why_still(rows)[:5]],
            "owners": [asdict(entry) for entry in owners(rows)],
            "running": running.to_dict() if running else None,
            "queue": asdict(queue.snapshot(robot)),
            "agent": agent_kpis(rows, [action for action in actions if action.get("robot") == robot]),
        }
    return {"day": day, "tz": store.tz, "fleet": asdict(fleet(rows_by_robot, now)), "robots": robots,
            "caveat": "hours moving is a proxy, not an objective; pair it with a throughput KPI"}


def _actions(root: Path) -> list[dict]:
    path = root / "agent" / "actions.jsonl"
    if not path.is_file():
        return []
    actions = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            actions.append(json.loads(line))
        except ValueError:
            continue
    return actions


def cmd_summary(args: argparse.Namespace) -> int:
    store = _store(args)
    day = _day(args, store)
    document = summary_document(store, Queue(store.root), day)
    if args.json:
        _print_json(document)
        return 0
    fleet = document["fleet"]
    print(f"{day}: fleet utilisation {fleet['utilization']:.0%} ({fleet['activity_h']:.1f} of {fleet['tracked_h']:.1f} "
          f"tracked h) · {fleet['robots_active_now']} of {fleet['robots']} robots active now, "
          f"{fleet['robots_moving_now']} moving")
    for robot, detail in document["robots"].items():
        split = detail["hours"]
        top = detail["why_dead"][0] if detail["why_dead"] else None
        top_text = f" · top dead reason: {top['reason']} ({top['hours']:.1f} h)" if top else ""
        print(f"  {robot}: activity {split['utilization']:.0%} · claimed {split['claimed_h']:.1f} h · moving "
              f"{split['moving_h']:.1f} h, still {split['still_h']:.1f} h, unknown {split['unknown_h']:.1f} h"
              f"{top_text}")
    print("  (hours moving is a proxy, not an objective; pair it with a throughput KPI)")
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    queue = Queue(_root(args))
    handler = {"list": _queue_list, "submit": _queue_submit, "policy": _queue_policy, "block": _queue_block,
               "unblock": _queue_unblock, "eta": _queue_eta, "start": _queue_start, "finish": _queue_finish,
               "cancel": _queue_cancel}[args.queue_command]
    return handler(queue, args)


def _queue_document(queue: Queue, robot: str | None) -> dict[str, Any]:
    if robot is not None:
        current = queue.running(robot)
        running = [current] if current else []
        runnable, waiting = queue.runnable(robot), queue.not_ready(robot)
    else:
        running = queue.items(status="running")
        queued = sorted(queue.items(status="queued"), key=lambda item: item.order)
        runnable = [item for item in queued if queue.is_ready(item.id)]
        waiting = [item for item in queued if not queue.is_ready(item.id)]
    return {"robot": robot, "running": [item.to_dict() for item in running],
            "runnable": [item.to_dict() for item in runnable],
            "not_ready": [{**item.to_dict(), "why_not_ready": queue.why_not_ready(item.id)} for item in waiting]}


def _queue_list(queue: Queue, args: argparse.Namespace) -> int:
    from robot_activity.views.terminal import render_queue

    if args.json:
        _print_json(_queue_document(queue, args.robot))
        return 0
    while True:
        text = render_queue(queue, robot=args.robot)
        if not args.watch:
            print(text, end="")
            return 0
        print("\x1b[2J\x1b[H" + text, end="", flush=True)  # --watch only: clear the screen between frames
        time.sleep(args.watch)


def _policy_from(args: argparse.Namespace, prefix: str = "policy_") -> PolicyRef | None:
    values = {name: getattr(args, f"{prefix}{name}", None) for name in ("repo", "commit", "path", "entrypoint",
                                                                         "sha256", "description")}
    if not any(values.values()):
        return None
    missing = [name for name in ("repo", "commit", "path") if not values[name]]
    if missing:
        raise UsageError(f"a policy needs repo, commit and path (missing: {', '.join(missing)})")
    return PolicyRef(**values)


def _done(item: Any, args: argparse.Namespace, verb: str) -> int:
    if args.json:
        _print_json(item.to_dict())
    else:
        print(f"{verb} {item.id} ({item.title}): {item.status}")
    return 0


def _queue_submit(queue: Queue, args: argparse.Namespace) -> int:
    blockers = []
    for text in args.block or ():
        kind, _, why = text.partition(":")
        blockers.append(Blocker(kind=kind.strip(), why=why.strip()))
    item = queue.submit(title=args.title, owner=args.owner, est_min=args.est_min, robot=args.robot,
                        executor=args.executor, source=args.source, priority=args.priority,
                        depends_on=tuple(args.depends_on or ()), policy=_policy_from(args), blockers=tuple(blockers),
                        eta_t=parse_time(args.eta), note=args.note, by=args.by)
    if args.json:
        _print_json(item.to_dict())
    else:
        print(item.id)  # bare id on stdout, so a script can capture it
    return 0


def _queue_policy(queue: Queue, args: argparse.Namespace) -> int:
    policy = _policy_from(args, prefix="")
    if policy is None:
        raise UsageError("queue policy needs --repo, --commit and --path")
    return _done(queue.set_policy(args.id, policy, by=args.by), args, "policy shared for")


def _queue_block(queue: Queue, args: argparse.Namespace) -> int:
    return _done(queue.block(args.id, args.kind, args.why, eta_t=parse_time(args.eta), by=args.by), args, "blocked")


def _queue_unblock(queue: Queue, args: argparse.Namespace) -> int:
    return _done(queue.unblock(args.id, args.kind, by=args.by), args, "unblocked")


def _queue_eta(queue: Queue, args: argparse.Namespace) -> int:
    return _done(queue.set_eta(args.id, parse_time(args.eta), by=args.by), args, "ETA set for")


def _queue_start(queue: Queue, args: argparse.Namespace) -> int:
    return _done(queue.start(args.id, robot=args.robot, by=args.by), args, "started")


def _queue_finish(queue: Queue, args: argparse.Namespace) -> int:
    return _done(queue.finish(args.id, result=args.result, note=args.note, by=args.by), args, "finished")


def _queue_cancel(queue: Queue, args: argparse.Namespace) -> int:
    return _done(queue.cancel(args.id, note=args.note, by=args.by), args, "cancelled")


def cmd_agent(args: argparse.Namespace) -> int:
    from robot_activity.agent import ActivityAgent, AgentConfig, action_record, load_config
    from robot_activity.notify import CommandNotifier, Outbox

    store = _store(args)
    config = load_config(args.config) if args.config else AgentConfig()
    notifier = None
    if args.notify_cmd:
        import shlex
        notifier = CommandNotifier(shlex.split(args.notify_cmd))
    elif args.outbox:
        notifier = Outbox(args.outbox)
    agent = ActivityAgent(store, Queue(store.root), config, notifier=notifier)
    while True:
        actions = agent.tick(time.time())
        if args.json:
            print(json.dumps([action_record(action) for action in actions]), flush=True)
        else:
            for action in actions:
                print(f"{action.kind} {action.robot} -> {', '.join(action.to)}: {action.message}", flush=True)
        if args.agent_command == "tick":
            return 0
        time.sleep(args.every)


def cmd_dashboard(args: argparse.Namespace) -> int:
    from robot_activity.views.html import dashboard

    store = _store(args)
    out = dashboard(store, Queue(store.root), _day(args, store), Path(args.out), now=parse_time(args.now),
                    until=parse_time(args.until))
    print(out)
    return 0


def cmd_wandb(args: argparse.Namespace) -> int:
    from robot_activity.wandb_log import push, report

    if args.wandb_command == "report":
        print(report(args.project, entity=args.entity, title=args.title, day=args.day))
        return 0
    store = _store(args)
    result = push(store, Queue(store.root), _day(args, store), args.project, entity=args.entity)
    _print_json(result)
    return 0


def cmd_sample(args: argparse.Namespace) -> int:
    from robot_activity.sampler import Observation, Sampler

    module_name, _, function_name = args.probe.partition(":")
    if not function_name:
        raise UsageError("--probe must be module:function")
    function = getattr(importlib.import_module(module_name), function_name)

    def probe() -> Observation:
        observation = function()
        return observation if isinstance(observation, Observation) else Observation(**observation)

    store = _store(args)
    sampler = Sampler(store, args.robot, probe, queue=Queue(store.root), sample_s=args.every)
    while True:
        started = time.time()
        try:
            sample = sampler.step(started)
            print(json.dumps(sample.to_dict()), flush=True)
        except Exception as error:  # noqa: BLE001 - a failing probe is an untracked window, never a crash
            print(f"robot-activity sample: probe failed, window untracked: {error!r}", file=sys.stderr, flush=True)
        if args.once:
            return 0
        time.sleep(max(0.0, args.every - (time.time() - started)))


# --- the parser -----------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="robot-activity",
                                     description="Robot Activity Agent: keep every robot doing useful work, and say "
                                                 "why when it isn't.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    demo = commands.add_parser("demo", help="write a simulated fleet day (samples, queue, agent actions)")
    _common(demo)
    demo.add_argument("--day", help="the simulated day (default: today)")
    demo.add_argument("--seed", type=int, default=0)
    demo.add_argument("--json", action="store_true")
    demo.set_defaults(handler=cmd_demo)

    summary = commands.add_parser("summary", help="the day's hours on the three splits, per robot and fleet")
    _common(summary)
    summary.add_argument("--day")
    summary.add_argument("--json", action="store_true")
    summary.set_defaults(handler=cmd_summary)

    _queue_parser(commands)

    agent = commands.add_parser("agent", help="run the agent's loop once (tick) or forever (run)")
    agent.add_argument("agent_command", choices=("tick", "run"))
    _common(agent)
    agent.add_argument("--config", help="agent.toml: thresholds and the program's declared fillers")
    agent.add_argument("--notify-cmd", help='a command that reads each action\'s JSON on stdin, e.g. "notify.sh"')
    agent.add_argument("--outbox", help="append each action to this JSON-lines file")
    agent.add_argument("--every", type=float, default=15.0, help="seconds between ticks for `run`")
    agent.add_argument("--json", action="store_true")
    agent.set_defaults(handler=cmd_agent)

    dashboard = commands.add_parser("dashboard", help="write the one-file HTML dashboard for a day")
    _common(dashboard)
    dashboard.add_argument("--day")
    dashboard.add_argument("--out", required=True)
    dashboard.add_argument("--now", help="render as of this time (default: now, or the day's end)")
    dashboard.add_argument("--until", help="cut every row and event after this time (replay frames)")
    dashboard.set_defaults(handler=cmd_dashboard)

    wandb = commands.add_parser("wandb", help="push a day to Weights & Biases (extra: robot-activity[wandb]), or "
                                              "save its Report (also needs wandb-workspaces)")
    wandb.add_argument("wandb_command", choices=("push", "report"))
    _common(wandb)
    wandb.add_argument("--project", required=True)
    wandb.add_argument("--entity")
    wandb.add_argument("--day", help="push: the day (default today); report: only that day's runs (default all)")
    wandb.add_argument("--title", default="Robot Activity Agent", help="report: its title (updated in place)")
    wandb.set_defaults(handler=cmd_wandb)

    sample = commands.add_parser("sample", help="write one Sample per window from your probe function")
    _common(sample)
    sample.add_argument("--robot", required=True)
    sample.add_argument("--probe", required=True, help="module:function returning an Observation (or its dict)")
    sample.add_argument("--every", type=float, default=10.0)
    sample.add_argument("--once", action="store_true", help="write one sample and exit")
    sample.set_defaults(handler=cmd_sample)
    return parser


def _queue_parser(commands: Any) -> None:
    queue = commands.add_parser("queue", help="list the queue, or change one item (owners and executors)")
    actions = queue.add_subparsers(dest="queue_command", required=True, metavar="ACTION")
    queue.set_defaults(handler=cmd_queue)

    def action(name: str, help_text: str, *, item: bool = True) -> argparse.ArgumentParser:
        parser = actions.add_parser(name, help=help_text)
        _common(parser)
        if item:
            parser.add_argument("id", help="the item id")
        parser.add_argument("--by", help="who is making this change (recorded in the ledger)")
        parser.add_argument("--json", action="store_true")
        return parser

    listing = actions.add_parser("list", help="the active queue (the default action)")
    _common(listing)
    listing.add_argument("--robot")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--watch", type=float, nargs="?", const=2.0, default=None, metavar="SECONDS")

    submit = action("submit", "add an item (owners only: it is yours)", item=False)
    submit.add_argument("--title", required=True)
    submit.add_argument("--owner", required=True)
    submit.add_argument("--est-min", type=float, required=True)
    submit.add_argument("--robot", default="any")
    submit.add_argument("--executor")
    submit.add_argument("--source", default="agent", choices=("system", "agent", "filler"))
    submit.add_argument("--priority", type=int)
    submit.add_argument("--depends-on", nargs="*")
    submit.add_argument("--block", action="append", metavar="KIND:WHY", help="e.g. code:IK refuses the bin pose")
    submit.add_argument("--eta", help="unix seconds, ISO 8601 or +90m")
    submit.add_argument("--note")
    for name in ("repo", "commit", "path", "entrypoint", "sha256", "description"):
        submit.add_argument(f"--policy-{name}", dest=f"policy_{name}")

    policy = action("policy", "share the code or policy the item will run (required before it is ready)")
    for name in ("repo", "commit", "path"):
        policy.add_argument(f"--{name}", required=True)
    for name in ("entrypoint", "sha256", "description"):
        policy.add_argument(f"--{name}")

    block = action("block", "say why the item cannot run yet")
    block.add_argument("--kind", required=True, choices=("code", "dependency", "human", "hardware", "data", "other"))
    block.add_argument("--why", required=True)
    block.add_argument("--eta")

    unblock = action("unblock", "clear one kind of blocker, or all")
    unblock.add_argument("--kind")

    eta = action("eta", "the owner's ETA for ready")
    eta.add_argument("--eta", required=True, help='unix seconds, ISO 8601, +90m, or "none"')

    start = action("start", "start a ready item on a robot (executors only)")
    start.add_argument("--robot", required=True)

    finish = action("finish", "finish a running item")
    finish.add_argument("--result", default="ok", help='"ok", or anything else for a failure')
    finish.add_argument("--note")

    cancel = action("cancel", "cancel a queued or running item")
    cancel.add_argument("--note")


def _normalise(argv: list[str]) -> list[str]:
    """`queue` alone, or followed by options, means `queue list`."""
    if argv and argv[0] == "queue" and (len(argv) == 1 or argv[1] not in QUEUE_COMMANDS and argv[1].startswith("-")
                                        and argv[1] not in ("-h", "--help")):
        return ["queue", "list", *argv[1:]]
    return argv


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalise(list(sys.argv[1:] if argv is None else argv)))
    if args.command == "queue" and args.queue_command == "start" and not args.by:
        parser.exit(2, "robot-activity: error: queue start needs --by (the executor starting it)\n")
    try:
        return args.handler(args)
    except (UsageError, QueueError, ValueError, KeyError, FileNotFoundError) as error:
        message = f"unknown item {error.args[0]}" if isinstance(error, KeyError) else str(error)
        print(f"robot-activity: error: {message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
