"""The dashboard: one self-contained HTML file per day (no network; inline CSS, SVG and a little JS).

Calm UI with progressive disclosure: one answer first (fleet utilisation today, robots active now), then the fleet now,
the day as a ribbon per robot, and the detail behind it on demand - per robot the three splits, why it stood still, who
held it, streaks and hand-off gaps; stalemate flags; the active queue (running, runnable order, not ready with why,
owner, ETA and the shared policy); and the agent's recent actions. Amber and red appear only for problems (dead time,
faults, stalemates), never as decoration. Light and dark follow the reader's system setting.

`until` cuts every row, event and action after that time, and the queue is replayed from its ledger as it stood then,
so a series of frames replays a day.
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

from robot_activity.agent import agent_kpis
from robot_activity.classify import classify, label
from robot_activity.metrics import (
    Hours,
    OwnerHours,
    ReasonHours,
    Streak,
    fleet,
    handoff_gaps,
    hours,
    owners,
    streaks,
    why_dead,
    why_still,
)
from robot_activity.queue import Item, PolicyRef, Queue, Replay, reasons_not_ready, replay
from robot_activity.schema import Sample
from robot_activity.stalemate import Flag, assess
from robot_activity.store import Store

HATCH_DEFS = ("<defs><pattern id=\"hatch\" width=\"5\" height=\"5\" patternUnits=\"userSpaceOnUse\" "
              "patternTransform=\"rotate(45)\"><rect class=\"hatch-line\" width=\"1.2\" height=\"5\"></rect>"
              "</pattern></defs>")
RIBBON_BIN_S = 120.0         # the ribbon summarises each 2-minute bin by its dominant state
STALEMATE_SCAN_S = 120.0     # the day is re-assessed for stalemates every 2 minutes
FRESH_S = 60.0
STATE_NAMES = {
    "moving": "activity · moving", "still": "activity · still", "unknown": "activity · no joint feed",
    "dead": "dead · claimed", "idle": "dead · unclaimed", "fault": "dead · fault",
}


# --- the page's data ------------------------------------------------------------------------------------------------

@dataclass
class Segment:
    start_t: float
    end_t: float
    state: str               # a key of STATE_NAMES
    detail: str              # what the tooltip says


@dataclass
class RobotView:
    name: str
    rows: list[Sample]
    split: Hours
    still_table: list[ReasonHours]
    dead_table: list[ReasonHours]
    owner_table: list[OwnerHours]
    dead_streaks: list[Streak]
    gaps: list[dict]
    segments: list[Segment]
    flags: list[Flag]
    running: Item | None
    next_up: Item | None
    kpis: dict
    now_state: str           # a key of STATE_NAMES, or "untracked"
    now_text: str


@dataclass
class Page:
    store: Store
    day: str
    day_start: float
    day_end: float
    now: float
    cut: float
    robots: list[RobotView]
    items: dict[str, Item]
    actions: list[dict]
    fleet_util: float = 0.0
    fleet_activity_h: float = 0.0
    fleet_tracked_h: float = 0.0
    active_now: int = 0
    moving_now: int = 0
    notes: list[str] = field(default_factory=list)

    def clock(self, t: float) -> str:
        return self.store.local(t).strftime("%H:%M")


def dashboard(store: Store, queue: Queue, day: str, out_path: str | Path, *, now: float | None = None,
              until: float | None = None) -> Path:
    """Write the day's dashboard to `out_path` (one file) and return its path."""
    page = _page(store, queue, day, now, until)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(f".{out.name}.tmp")
    temporary.write_text(_render(page), encoding="utf-8")
    os.replace(temporary, out)
    return out


def _page(store: Store, queue: Queue, day: str, now: float | None, until: float | None) -> Page:
    day_start, day_end = store.day_bounds(day)
    cut = math.inf if until is None else until
    rows_by_robot = {robot: [row for row in store.rows(robot, day) if row.t <= cut] for robot in store.robots()}
    rows_by_robot = {robot: rows for robot, rows in rows_by_robot.items() if rows}
    if now is None:
        latest = max((rows[-1].t for rows in rows_by_robot.values()), default=day_start)
        now = until if until is not None else max(latest, min(time.time(), day_end))
    events = [event for event in queue.events() if event["t"] <= cut]
    items = replay(events)
    actions = [action for action in _read_actions(store.root) if action.get("t", 0) <= cut
               and day_start <= action.get("t", 0) < day_end]
    flags = _stalemate_history(rows_by_robot, events, day_start, min(now, day_end))
    robots = [_robot_view(store, robot, rows, items, events, actions, flags.get(robot, []), now, day_start, day_end)
              for robot, rows in rows_by_robot.items()]
    summary = fleet(rows_by_robot, now, fresh_s=FRESH_S)
    return Page(store=store, day=day, day_start=day_start, day_end=day_end, now=now, cut=cut, robots=robots,
                items=items, actions=actions, fleet_util=summary.utilization, fleet_activity_h=summary.activity_h,
                fleet_tracked_h=summary.tracked_h, active_now=summary.robots_active_now,
                moving_now=summary.robots_moving_now)


def _read_actions(root: Path) -> list[dict]:
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


def _robot_view(store: Store, robot: str, rows: list[Sample], items: dict[str, Item], events: list[dict],
                actions: list[dict], flags: list[Flag], now: float, day_start: float, day_end: float) -> RobotView:
    running = next((item for item in items.values() if item.status == "running" and item.ran_on == robot), None)
    queued = sorted((item for item in items.values() if item.status == "queued" and item.fits(robot)),
                    key=lambda item: item.order)
    next_up = next((item for item in queued if not reasons_not_ready(item, items)), None)
    gaps = [gap for gap in handoff_gaps(events, robot) if day_start <= gap["ended_t"] < day_end]
    dead = sorted(streaks(rows, "dead"), key=lambda streak: -streak.seconds)
    robot_actions = [action for action in actions if action.get("robot") == robot]
    now_state, now_text = _now_state(rows, now)
    return RobotView(
        name=robot, rows=rows, split=hours(rows), still_table=why_still(rows), dead_table=why_dead(rows),
        owner_table=owners(rows), dead_streaks=dead[:3], gaps=gaps, segments=_segments(store, rows), flags=flags,
        running=running, next_up=next_up, kpis=agent_kpis(rows, robot_actions), now_state=now_state,
        now_text=now_text)


def _now_state(rows: list[Sample], now: float) -> tuple[str, str]:
    latest = rows[-1]
    if now - latest.t > FRESH_S:
        return "untracked", f"no rows for {_duration(now - latest.t)} (untracked, never idle)"
    classified = classify(latest)
    state = _state(latest)
    if classified.activity:
        return state, STATE_NAMES[state]
    dead = streaks(rows, "dead")
    streak_s = dead[-1].seconds if dead and dead[-1].end_t == latest.t else latest.sample_s
    return state, f"dead {_duration(streak_s)} · {label(classified)}"


def _state(row: Sample) -> str:
    classified = classify(row)
    if classified.activity:
        return classified.motion
    if row.health != "ok":
        return "fault"
    return "dead" if classified.claimed else "idle"


def _segments(store: Store, rows: list[Sample]) -> list[Segment]:
    """Rows summarised into bins (dominant state), then equal neighbouring bins merged; gaps stay empty."""
    bins: dict[int, dict[str, float]] = {}
    labels: dict[int, dict[str, float]] = {}
    items: dict[int, dict[str, float]] = {}
    for row in rows:
        index = int((row.t - 1e-6) // RIBBON_BIN_S)
        bins.setdefault(index, {}).setdefault(_state(row), 0.0)
        bins[index][_state(row)] += row.sample_s
        labels.setdefault(index, {}).setdefault(label(classify(row)), 0.0)
        labels[index][label(classify(row))] += row.sample_s
        if row.item:
            items.setdefault(index, {}).setdefault(row.item, 0.0)
            items[index][row.item] += row.sample_s
    segments: list[Segment] = []
    for index in sorted(bins):
        state = max(bins[index].items(), key=lambda entry: entry[1])[0]
        reason = max(labels[index].items(), key=lambda entry: entry[1])[0]
        item = max(items[index].items(), key=lambda entry: entry[1])[0] if index in items else None
        start_t, end_t = index * RIBBON_BIN_S, (index + 1) * RIBBON_BIN_S
        detail = reason + (f" · {item}" if item else "")
        previous = segments[-1] if segments else None
        if previous and previous.end_t == start_t and previous.state == state and previous.detail == detail:
            previous.end_t = end_t
        else:
            segments.append(Segment(start_t, end_t, state, detail))
    return segments


def _stalemate_history(rows_by_robot: dict[str, list[Sample]], events: list[dict], day_start: float,
                       until: float) -> dict[str, list[Flag]]:
    """Every distinct stalemate flag of the day, from re-assessing the rows against the queue as it stood then."""
    found: dict[str, dict[tuple, Flag]] = {robot: {} for robot in rows_by_robot}
    ledger = Replay(events)
    times = {robot: [row.t for row in rows] for robot, rows in rows_by_robot.items()}
    scan_t = day_start + STALEMATE_SCAN_S
    while scan_t <= until + STALEMATE_SCAN_S:
        moment = min(scan_t, until)
        items = list(ledger.at(moment).values())
        for robot, rows in rows_by_robot.items():
            first = _bisect(times[robot], moment - 700.0)
            last = _bisect(times[robot], moment)
            for flag in assess(rows[first:last], items, moment):
                found[robot].setdefault((flag.kind, flag.item), flag)
        scan_t += STALEMATE_SCAN_S
    return {robot: sorted(flags.values(), key=lambda flag: flag.since_t) for robot, flags in found.items()}


def _bisect(times: list[float], t: float) -> int:
    low, high = 0, len(times)
    while low < high:
        middle = (low + high) // 2
        if times[middle] <= t:
            low = middle + 1
        else:
            high = middle
    return low


# --- formatting -----------------------------------------------------------------------------------------------------

def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def _h(hours_value: float) -> str:
    if hours_value == 0:
        return "0"
    return f"{hours_value:.1f} h" if hours_value >= 0.05 else f"{hours_value * 60:.0f} min"


def _pct(share: float) -> str:
    return f"{share * 100:.0f}%"


def _e(text: object) -> str:
    return escape(str(text), quote=True)


def _family(reason: str) -> str:
    """Group reasons that differ only in a count, e.g. "queue waiting (3 runnable, not started)"."""
    return re.sub(r"\(\d+ runnable, not started\)", "(n runnable, not started)", reason)


def _is_problem(reason: str) -> bool:
    return not reason.startswith("in activity")


# --- rendering ------------------------------------------------------------------------------------------------------

def _render(page: Page) -> str:
    title = f"Robot activity · {page.day}"
    body = "".join([_header(page), _hero(page), _fleet_now(page), _ribbon(page), _robots(page), _stalemates(page),
                    _queue_section(page), _agent_section(page), _footer(page)])
    return (f"<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<meta name=\"color-scheme\" content=\"light dark\">\n<title>{_e(title)}</title>\n"
            f"<style>{CSS}</style>\n</head>\n<body>\n<main>\n{body}\n</main>\n"
            f"<div class=\"tip\" id=\"tip\" role=\"tooltip\" hidden></div>\n<script>{SCRIPT}</script>\n</body>\n</html>\n")


def _header(page: Page) -> str:
    zone = page.store.tz or time.strftime("%Z")
    as_of = page.clock(min(page.now, page.cut))
    return (f"<header class=\"top\"><div><h1>Robot activity</h1><p class=\"sub\">{_e(page.day)} · "
            f"{len(page.robots)} robot{'s' if len(page.robots) != 1 else ''} · as of {_e(as_of)} {_e(zone)}</p></div>"
            f"<button class=\"ghost\" type=\"button\" data-expand>Expand all</button></header>")


def _hero(page: Page) -> str:
    open_flags = [flag for robot in page.robots for flag in robot.flags if robot.running and flag.item == robot.running.id]
    dead_now = [robot for robot in page.robots if robot.now_state in ("dead", "idle", "fault")]
    attention = []
    if open_flags:
        attention.append(f"{len(open_flags)} stalemate{'s' if len(open_flags) != 1 else ''} open")
    attention.extend(f"{robot.name}: {robot.now_text}" for robot in dead_now)
    attention_html = ""
    if attention:
        attention_html = ("<ul class=\"attention\" aria-label=\"Needs a look\">"
                          + "".join(f"<li><span class=\"dot warn\" aria-hidden=\"true\"></span>{_e(text)}</li>"
                                    for text in attention) + "</ul>")
    robots = len(page.robots)
    return (
        "<section class=\"card hero\" aria-label=\"Fleet today\">"
        "<div class=\"hero-main\">"
        f"<div class=\"figure\">{_pct(page.fleet_util)}</div>"
        "<div class=\"figure-label\">fleet utilisation today</div>"
        f"<div class=\"figure-note\">{_h(page.fleet_activity_h)} of activity in {_h(page.fleet_tracked_h)} tracked "
        "(activity ÷ tracked time, all calendar hours)</div></div>"
        "<div class=\"hero-side\">"
        f"<div class=\"stat\"><span class=\"stat-value\">{page.active_now}<span class=\"of\"> of {robots}</span></span>"
        "<span class=\"stat-label\">robots active now</span></div>"
        f"<div class=\"stat\"><span class=\"stat-value\">{page.moving_now}<span class=\"of\"> of {robots}</span></span>"
        "<span class=\"stat-label\">moving now</span></div></div>"
        f"{attention_html}</section>")


def _thin_bar(parts: list[tuple[str, float, str]], name: str) -> str:
    """A thin segmented bar: one whole split into parts (class, share, text for the accessible name)."""
    spans = "".join(f"<span class=\"seg {cls}\" style=\"flex-grow:{share:.5f}\" title=\"{_e(text)}\"></span>"
                    for cls, share, text in parts if share > 0)
    described = "; ".join(text for _, share, text in parts if share > 0)
    return f"<div class=\"thin\" role=\"img\" aria-label=\"{_e(name)}: {_e(described)}\">{spans}</div>"


def _fleet_now(page: Page) -> str:
    cards = []
    for robot in page.robots:
        split = robot.split
        dot = {"moving": "act", "still": "act-still", "unknown": "act-still", "dead": "warn", "idle": "warn",
               "fault": "bad", "untracked": "muted"}[robot.now_state]
        running = (f"<p class=\"line\"><span class=\"k\">running</span> {_e(robot.running.title)} · "
                   f"{_duration(page.now - (robot.running.started_t or page.now))} of est "
                   f"{robot.running.est_min:g} min</p>") if robot.running else ""
        next_up = (f"<p class=\"line\"><span class=\"k\">next</span> {_e(robot.next_up.title)} · "
                   f"{_e(robot.next_up.executor)}</p>") if robot.next_up else ""
        bar = _thin_bar([("act", split.utilization, f"activity {_pct(split.utilization)}"),
                         ("dead", 1 - split.utilization if split.tracked_h else 0, f"dead {_pct(1 - split.utilization)}")],
                        f"{robot.name} today")
        cards.append(
            f"<article class=\"robot-card\"><h3>{_e(robot.name)}</h3>"
            f"<p class=\"state\"><span class=\"dot {dot}\" aria-hidden=\"true\"></span>{_e(robot.now_text)}</p>"
            f"<div class=\"util\"><span class=\"util-value\">{_pct(split.utilization)}</span>"
            f"<span class=\"util-label\">active today</span></div>{bar}{running}{next_up}</article>")
    return f"<section aria-label=\"Fleet now\"><h2>Fleet now</h2><div class=\"cards\">{''.join(cards)}</div></section>"


def _ribbon(page: Page) -> str:
    span = page.day_end - page.day_start
    width = 1000.0

    def x(t: float) -> float:
        return max(0.0, min(width, (t - page.day_start) / span * width))

    lanes = []
    for robot in page.robots:
        rects = []
        for segment in robot.segments:
            start, end = x(segment.start_t), x(segment.end_t)
            if end <= start:
                continue
            tip = (f"{page.clock(segment.start_t)}–{page.clock(segment.end_t)} · {STATE_NAMES[segment.state]} · "
                   f"{segment.detail}")
            rects.append(f"<rect class=\"r-{segment.state}\" x=\"{start:.2f}\" y=\"6\" width=\"{end - start:.2f}\" "
                         f"height=\"22\" data-tip=\"{_e(tip)}\"></rect>")
            if segment.state == "unknown":  # no joint feed: hatched, so it never reads as still
                rects.append(f"<rect class=\"hatch\" x=\"{start:.2f}\" y=\"6\" width=\"{end - start:.2f}\" "
                             "height=\"22\"></rect>")
        for flag in robot.flags:
            flag_x = x(flag.since_t)
            tip = f"{page.clock(flag.since_t)} · stalemate · {flag.kind}: {flag.message}"
            rects.append(f"<path class=\"flag\" d=\"M{flag_x - 4:.2f},0 L{flag_x + 4:.2f},0 L{flag_x:.2f},6 Z\" "
                         f"data-tip=\"{_e(tip)}\"></path>")
        if page.day_start <= page.now <= page.day_end - RIBBON_BIN_S:
            rects.append(f"<line class=\"now\" x1=\"{x(page.now):.2f}\" x2=\"{x(page.now):.2f}\" y1=\"2\" y2=\"32\">"
                         "</line>")
        lanes.append(f"<div class=\"lane\"><div class=\"lane-name\">{_e(robot.name)}</div>"
                     f"<svg class=\"lane-svg\" viewBox=\"0 0 {width:.0f} 34\" preserveAspectRatio=\"none\" "
                     f"role=\"img\" aria-label=\"{_e(robot.name)} over the day\">{''.join(rects)}</svg>"
                     "</div>")
    ticks = "".join(f"<span style=\"left:{x(page.day_start + hour * 3600) / width * 100:.2f}%\">"
                    f"{_e(page.clock(page.day_start + hour * 3600))}</span>"
                    for hour in range(0, round(span / 3600) + 1, 6))
    legend = "".join(f"<span class=\"key\"><span class=\"swatch r-{state}\"></span>{_e(name)}</span>"
                     for state, name in STATE_NAMES.items())
    legend += "<span class=\"key\"><span class=\"swatch flag-key\"></span>stalemate flag</span>"
    return ("<section class=\"card\" aria-label=\"The day\"><h2>The day</h2>"
            "<p class=\"hint\">One lane per robot, each 2-minute bin coloured by its main state. Hover for the reason. "
            "Blank means untracked (no rows), never idle.</p>"
            f"<svg class=\"defs\" width=\"0\" height=\"0\" aria-hidden=\"true\">{HATCH_DEFS}</svg>"
            f"<div class=\"legend\">{legend}</div><div class=\"ribbon\" data-ribbon>{''.join(lanes)}"
            f"<div class=\"axis\">{ticks}</div><div class=\"crosshair\" hidden></div></div></section>")


def _split_bar(name: str, parts: list[tuple[str, str, float]], total_h: float) -> str:
    """A tall segmented bar for one split: parts are (class, label, hours)."""
    blocks = []
    for cls, text, value in parts:
        share = value / total_h if total_h else 0.0
        if share <= 0:
            continue
        inside = f"<span class=\"in\">{_pct(share)}</span>" if share >= 0.09 else ""
        blocks.append(f"<span class=\"block {cls}\" style=\"flex-grow:{share:.5f}\" "
                      f"title=\"{_e(text)}: {_h(value)} ({_pct(share)})\">{inside}</span>")
    keys = "".join(f"<span class=\"key\"><span class=\"swatch {cls}\"></span>{_e(text)} {_h(value)}</span>"
                   for cls, text, value in parts)
    return (f"<div class=\"split\"><div class=\"split-name\">{_e(name)}</div>"
            f"<div class=\"tall\" role=\"img\" aria-label=\"{_e(name)}\">{''.join(blocks)}</div>"
            f"<div class=\"legend small\">{keys}</div></div>")


def _reason_rows(table: list[ReasonHours], *, problem_class: str = "warn") -> str:
    """Reason table rows; reasons differing only in a count are grouped, their exact labels one click away."""
    families: dict[str, list[ReasonHours]] = {}
    for entry in table:
        families.setdefault(_family(entry.reason), []).append(entry)
    total = sum(entry.hours for entry in table) or 1.0
    ranked = sorted(families.items(), key=lambda family: -sum(entry.hours for entry in family[1]))
    peak = max((sum(entry.hours for entry in members) for _, members in ranked), default=1.0) or 1.0
    rows = []
    for family, members in ranked:
        family_h = sum(entry.hours for entry in members)
        cls = problem_class if _is_problem(family) else "act-still"
        if family.startswith(("fault", "BUS DOWN", "disconnected", "estop")):
            cls = "bad"
        exact = ""
        if len(members) > 1 or members[0].reason != family:
            exact = ("<details class=\"exact\"><summary>exact labels</summary><ul>"
                     + "".join(f"<li>{_e(entry.reason)} · {_h(entry.hours)}</li>" for entry in members)
                     + "</ul></details>")
        rows.append(f"<tr><td class=\"reason\">{_e(family)}{exact}</td>"
                    f"<td class=\"num\">{_h(family_h)}</td><td class=\"num\">{_pct(family_h / total)}</td>"
                    f"<td class=\"barcell\"><span class=\"hbar {cls}\" style=\"width:{family_h / peak * 100:.1f}%\">"
                    "</span></td></tr>")
    return "".join(rows)


def _table(title: str, head: str, body: str, empty: str, *, narrow: bool = False, stack: bool = False) -> str:
    """A titled table panel. `stack`: on a phone each row becomes a card of labelled lines (cells carry data-label)."""
    if not body:
        return f"<div class=\"panel\"><h4>{_e(title)}</h4><p class=\"calm\">{_e(empty)}</p></div>"
    panel_class = "panel narrow" if narrow else "panel"
    table_class = ' class="stack"' if stack else ""
    return (f"<div class=\"{panel_class}\"><h4>{_e(title)}</h4><div class=\"scroll\">"
            f"<table{table_class}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div></div>")


def _row(cells: list[tuple[str, str, str]]) -> str:
    """One table row from (label, html, css class) cells; the label shows beside the value when rows stack."""
    return "<tr>" + "".join(f"<td class=\"{cls}\" data-label=\"{_e(label)}\">{content}</td>"
                            for label, content, cls in cells) + "</tr>"


def _robots(page: Page) -> str:
    sections = []
    for robot in page.robots:
        split = robot.split
        total = split.tracked_h
        bars = "".join([
            _split_bar("Claim", [("claimed", "claimed", split.claimed_h), ("idle", "unclaimed", split.unclaimed_h)],
                       total),
            _split_bar("Activity", [("act", "activity", split.activity_h), ("dead", "dead", split.dead_h)], total),
            _split_bar("Motion", [("act", "moving", split.moving_h), ("act-still", "still", split.still_h),
                                  ("unknown", "unknown", split.unknown_h)], total),
        ])
        reason_head = "<th>reason</th><th class=\"num\">hours</th><th class=\"num\">share</th><th></th>"
        still = _table("Why it stood still", reason_head, _reason_rows(robot.still_table), "It never stood still.")
        dead = _table("Why it was dead", reason_head, _reason_rows(robot.dead_table), "No dead time.")
        owner_rows = "".join(
            f"<tr><td>{_e(entry.owner)}</td><td class=\"num\">{_h(entry.hours)}</td>"
            f"<td class=\"num\">{_h(entry.moving_h)}</td><td class=\"num\">{_h(entry.still_h)}</td>"
            f"<td class=\"num\">{_h(entry.unknown_h)}</td></tr>" for entry in robot.owner_table)
        held = _table("Who held the robot", "<th>owner</th><th class=\"num\">hours</th><th class=\"num\">moving</th>"
                      "<th class=\"num\">still</th><th class=\"num\">unknown</th>", owner_rows, "Nobody held it.")
        streak_rows = "".join(
            f"<tr><td class=\"when\">{_e(page.clock(max(streak.start_t, page.day_start)))}–"
            f"{_e(page.clock(streak.end_t))}</td>"
            f"<td class=\"num\">{_duration(streak.seconds)}</td><td>{_e(_mostly(streak))}</td></tr>"
            for streak in robot.dead_streaks)
        streaks_html = _table("Longest dead streaks", "<th>when</th><th class=\"num\">for</th><th>mostly</th>",
                              streak_rows, "No dead streaks.")
        gaps_html = _gaps(page, robot)
        top_dead = next(iter(robot.dead_table), None)
        top = (f"<span class=\"summary-reason\"><span class=\"dot warn\" aria-hidden=\"true\"></span>"
               f"<span>top dead reason: {_e(_family(top_dead.reason))} · "
               f"<span class=\"nowrap\">{_h(top_dead.hours)}</span></span></span>") if top_dead else ""
        sections.append(
            f"<details class=\"robot\"><summary><span class=\"summary-head\"><span class=\"summary-name\">"
            f"{_e(robot.name)}</span><span class=\"summary-util\">{_pct(split.utilization)} active · "
            f"{_pct(split.moving_share)} moving</span></span>{top}</summary><div class=\"robot-body\"><div class=\"splits\">{bars}</div>"
            f"<div class=\"panels\">{still}{dead}{held}{streaks_html}{gaps_html}</div></div></details>")
    return (f"<section class=\"card\" aria-label=\"Robots\"><h2>Robots</h2><p class=\"hint\">The three splits of "
            f"every tracked hour, and the reasons behind them. Unknown (no joint feed) is never counted as idle.</p>"
            f"{''.join(sections)}</section>")


def _gaps(page: Page, robot: RobotView) -> str:
    if not robot.gaps:
        return _table("Hand-off gaps", "", "", "No hand-offs yet: a gap is the end of one item to the start of the next.")
    gaps_s = [gap["gap_s"] for gap in robot.gaps]
    slowest = sorted(robot.gaps, key=lambda gap: -gap["gap_s"])[:3]
    rows = "".join(f"<tr><td>{_e(page.clock(gap['ended_t']))}</td><td>{_e(_title(page, gap['after']))} → "
                   f"{_e(_title(page, gap['next']))}</td><td class=\"num\">{_duration(gap['gap_s'])}</td></tr>"
                   for gap in slowest)
    summary = (f"<p class=\"calm\">{len(gaps_s)} hand-offs · median {_duration(statistics.median(gaps_s))} · "
               f"longest {_duration(max(gaps_s))}</p>")
    return (f"<div class=\"panel\"><h4>Hand-off gaps</h4>{summary}<div class=\"scroll\"><table><thead><tr>"
            f"<th>ended</th><th>slowest hand-offs</th><th class=\"num\">gap</th></tr></thead><tbody>{rows}"
            "</tbody></table></div></div>")


def _title(page: Page, item_id: str) -> str:
    item = page.items.get(item_id)
    return item.title if item else item_id


def _stalemates(page: Page) -> str:
    flags = sorted(((robot, flag) for robot in page.robots for flag in robot.flags), key=lambda pair: pair[1].since_t)
    if not flags:
        return ("<section class=\"card\" aria-label=\"Stalemates\"><h2>Stalemates</h2>"
                "<p class=\"calm\">None today. No robot was stuck, looping in place or past its estimate.</p></section>")
    rows = []
    for robot, flag in flags:
        item = page.items.get(flag.item) if flag.item else None
        policy = (f"<div class=\"policy\"><span class=\"k\">policy</span> {_policy_html(item.policy)}</div>"
                  if item and item.policy else "")
        outcome = f" · now {_e(item.status)}" if item else ""
        rows.append(f"<li class=\"flag-row\"><span class=\"dot warn\" aria-hidden=\"true\"></span><div>"
                    f"<div><strong>{_e(robot.name)} · {_e(flag.kind)}</strong> <span class=\"muted\">since "
                    f"{_e(page.clock(flag.since_t))}{outcome}</span></div><div>{_e(flag.message)}</div>{policy}"
                    "</div></li>")
    return ("<section class=\"card\" aria-label=\"Stalemates\"><h2>Stalemates</h2>"
            "<p class=\"hint\">Motion that looks like work. Each flag is a sign to look, not a verdict: open the shared "
            f"policy to judge whether the behaviour is expected.</p><ul class=\"flags\">{''.join(rows)}</ul></section>")


def _eta(page: Page, item: Item) -> str:
    etas = [eta for eta in (item.eta_t, *(blocker.eta_t for blocker in item.blockers)) if eta is not None]
    if not etas:
        return "no ETA"
    eta = max(etas)
    overdue = " (overdue)" if eta < page.now else ""
    day = "" if page.store.day_of(eta) == page.day else f"{page.store.day_of(eta)} "
    return f"ETA {day}{page.clock(eta)}{overdue}"


def _queue_section(page: Page) -> str:
    items = page.items
    running = sorted((item for item in items.values() if item.status == "running"), key=lambda item: item.ran_on or "")
    queued = sorted((item for item in items.values() if item.status == "queued"), key=lambda item: item.order)
    ready = [item for item in queued if not reasons_not_ready(item, items)]
    waiting = [item for item in queued if reasons_not_ready(item, items)]

    def policy(item: Item) -> str:
        return _policy_html(item.policy) if item.policy else "<span class=\"muted\">none shared</span>"

    def title(item: Item) -> str:
        return f"{_e(item.title)}<div class=\"id\">{_e(item.id)}</div>"

    def owner(item: Item) -> str:
        return _e(item.owner) + ("" if item.executor == item.owner else f" → {_e(item.executor)}")

    running_rows = "".join(_row([
        ("robot", _e(item.ran_on), "nowrap"), ("item", title(item), "lead"), ("owner", owner(item), ""),
        ("elapsed / est", f"{_duration(page.now - (item.started_t or page.now))} / {item.est_min:g} min", "num"),
        ("policy", policy(item), "policy-cell")]) for item in running)
    ready_rows = "".join(_row([
        ("#", str(position), "num"), ("item", title(item), "lead"), ("robot", _e(item.robot), "nowrap"),
        ("source", f"{_e(item.source)} · p{item.priority}", ""), ("owner → executor", owner(item), ""),
        ("est", f"{item.est_min:g} min", "num"), ("policy", policy(item), "policy-cell")])
        for position, item in enumerate(ready, start=1))
    waiting_rows = "".join(_row([
        ("item", title(item), "lead"), ("robot", _e(item.robot), "nowrap"),
        ("why", f"<div class=\"reasons\">{_why_cell(reasons_not_ready(item, items))}</div>", "why"), ("owner", owner(item), ""),
        ("ETA", _e(_eta(page, item)), ""), ("policy", policy(item), "policy-cell")]) for item in waiting)
    return (
        "<section class=\"card\" aria-label=\"The active queue\"><h2>The active queue</h2>"
        "<p class=\"hint\">Items belong to their owners. One runs per robot; the rest start in priority order (system "
        "0, agent 5, filler 9), and only once the owner has shared the policy it will run.</p>"
        + _table("Running now", "<th>robot</th><th>item</th><th>owner → executor</th>"
                 "<th class=\"num\">elapsed / est</th><th>policy</th>", running_rows, "Nothing is running.", stack=True)
        + _table("Runnable, in start order", "<th class=\"num\">#</th><th>item</th><th>robot</th><th>source</th>"
                 "<th>owner → executor</th><th class=\"num\">est</th><th>policy</th>", ready_rows,
                 "Nothing is runnable: the queue is dry.", stack=True)
        + _table("Not ready", "<th>item</th><th>robot</th><th>why</th><th>owner → executor</th><th>ETA</th>"
                 "<th>policy</th>", waiting_rows, "Every queued item is ready.", stack=True)
        + "</section>")


def _policy_html(policy: PolicyRef) -> str:
    """The shared policy, short enough for a table cell; the full ref on hover; a link to the exact file on GitHub."""
    full = policy.describe()
    repo_name = policy.repo.rstrip("/").rsplit("/", 1)[-1] if "://" in policy.repo else policy.repo
    short = f"{repo_name}@{policy.commit[:12]}:{policy.path}" + (f"::{policy.entrypoint}" if policy.entrypoint else "")
    text = f"<code title=\"{_e(full)}\">{_e(short)}</code>"
    if policy.repo.startswith("https://github.com/"):
        url = f"{policy.repo.rstrip('/')}/blob/{policy.commit}/{policy.path}"
        return f"<a class=\"policy-link\" href=\"{_e(url)}\" rel=\"noopener\">{text}</a>"
    if policy.repo.startswith(("https://", "http://")):
        return f"<a class=\"policy-link\" href=\"{_e(policy.repo)}\" rel=\"noopener\">{text}</a>"
    return text


def _why_cell(reasons: list[str]) -> str:
    return "".join(f"<div><span class=\"dot warn\" aria-hidden=\"true\"></span>{_e(reason)}</div>" for reason in reasons)


def _mostly(streak: Streak) -> str:
    """A dead streak's top two reasons with their durations."""
    top = [entry for entry in sorted(streak.reasons.items(), key=lambda entry: -entry[1])[:2]
           if entry[1] >= 0.1 * streak.seconds]
    if len(top) == 1:
        return top[0][0]
    return ", ".join(f"{reason} {_duration(seconds)}" for reason, seconds in top)


def _agent_section(page: Page) -> str:
    kpi_rows, notes = [], []
    for robot in page.robots:
        unprompted = robot.kpis["idle_unprompted_s"]
        cls = "warn" if unprompted >= 60 else "act"
        if unprompted >= 15 * 60:
            notes.append(f"<li><span class=\"dot warn\" aria-hidden=\"true\"></span><span>{_e(robot.name)} sat dead "
                         f"{_duration(unprompted)} with nobody prompted, mostly "
                         f"{_e(_family(robot.dead_table[0].reason)) if robot.dead_table else 'dead'}: with nothing "
                         "runnable, no not-ready owner to ask and no declared filler, the agent had nobody to ask. "
                         "Declare a filler for it.</span></li>")
        kpi_rows.append(f"<tr><td>{_e(robot.name)}</td><td class=\"num\">{_h(robot.kpis['dead_h'])}</td>"
                        f"<td class=\"num\"><span class=\"dot {cls}\" aria-hidden=\"true\"></span>"
                        f"{_duration(unprompted)}</td><td class=\"num\">{robot.kpis['prompts']}</td></tr>")
    notes_html = f"<ul class=\"notes\">{''.join(notes)}</ul>" if notes else ""
    groups: dict[tuple, list[dict]] = {}
    for action in page.actions:  # one line per kind of prompt per robot: the latest, and how often today
        groups.setdefault((action.get("kind"), action.get("robot")), []).append(action)
    recent = sorted(groups.values(), key=lambda group: -group[-1]["t"])
    rows = []
    for group in recent[:12]:
        last = group[-1]
        repeat = f" · ×{len(group)} today, first {_e(page.clock(group[0]['t']))}" if len(group) > 1 else ""
        cls = "warn" if last.get("kind") == "stalemate" else "act-still"
        rows.append(
            f"<li class=\"action\"><span class=\"dot {cls}\" aria-hidden=\"true\"></span><div>"
            f"<div><strong>{_e(last.get('kind'))}</strong> · {_e(last.get('robot'))} → "
            f"{_e(', '.join(last.get('to') or []))} <span class=\"muted\">{_e(page.clock(last['t']))}{repeat}</span>"
            f"</div><div class=\"message\">{_e(last.get('message'))}</div></div></li>")
    more = ("<p class=\"calm\">Every action, with its full message, is in <code>agent/actions.jsonl</code>.</p>")
    actions_html = (f"<ul class=\"actions\">{''.join(rows)}</ul>{more}" if rows
                    else "<p class=\"calm\">The agent has not acted today.</p>")
    return (
        "<section class=\"card\" aria-label=\"The agent\"><h2>The agent</h2>"
        "<p class=\"hint\">It prompts executors, asks owners for ETAs, enqueues the program's declared fillers and "
        "flags stalemates. It never decides what the work is. Its own KPI: dead time past 3 min with nobody "
        "prompted, target zero.</p>"
        + _table("Its KPI today",  "<th>robot</th><th class=\"num\">dead</th><th class=\"num\">idle unprompted</th>"
                 "<th class=\"num\">actions</th>", "".join(kpi_rows), "No robots.", narrow=True)
        + notes_html
        + f"<div class=\"panel\"><h4>Recent actions</h4>{actions_html}</div></section>")


def _footer(page: Page) -> str:
    return ("<footer><p><strong>Hours moving is a proxy, not an objective.</strong> Some idle time is slack a "
            "bottleneck needs (Theory of Constraints); pair this page with a throughput KPI such as useful picks per "
            "day. Utilisation here is activity ÷ tracked calendar time (TEEP-style availability).</p>"
            f"<p class=\"muted\">robot-activity · samples <code>robot-activity/1</code> · ledger "
            f"<code>robot-activity-event/1</code> · {_e(page.day)}</p></footer>")


# --- style and behaviour --------------------------------------------------------------------------------------------

CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--raised:#ffffff;--ink:#0b0b0b;--ink-2:#52514e;
--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--ring:rgba(11,11,11,.10);--act:#2a78d6;--act-still:#86b6ef;
--act-unknown:#cde2fb;--hatch:#6da7ec;--claimed:#52514e;--dead:#c3c2b7;--idle:#e6e5df;--warn:#fab219;
--bad:#d03b3b;--hover:rgba(11,11,11,.04)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#0d0d0d;
--surface:#1a1a19;--raised:#20201f;--ink:#f0efec;--ink-2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
--ring:rgba(255,255,255,.10);--act:#3987e5;--act-still:#1c5cab;--act-unknown:#13345e;--hatch:#3987e5;
--claimed:#c3c2b7;--dead:#52514e;--idle:#2c2c2a;--hover:rgba(255,255,255,.05)}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--raised:#20201f;--ink:#f0efec;
--ink-2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);--act:#3987e5;
--act-still:#1c5cab;--act-unknown:#13345e;--hatch:#3987e5;--claimed:#c3c2b7;--dead:#52514e;--idle:#2c2c2a;
--hover:rgba(255,255,255,.05)}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1120px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0;font-weight:650;letter-spacing:-.01em}
h2{font-size:17px;margin:0 0 4px;font-weight:650}
h3{font-size:15px;margin:0 0 6px;font-weight:650}
h4{font-size:13px;margin:0 0 8px;font-weight:600;color:var(--ink-2);text-transform:uppercase;letter-spacing:.04em}
p{margin:0}
code{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink-2);overflow-wrap:anywhere}
section{margin-top:16px}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:14px;padding:20px}
.top{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;margin-bottom:4px}
.sub,.hint,.muted{color:var(--ink-2)}
.hint{font-size:13px;margin-bottom:14px;max-width:72ch}
.calm{color:var(--ink-2);font-size:14px}
.small-text{font-size:12px;font-weight:400}
.nowrap{white-space:nowrap}
.ghost{white-space:nowrap;font:inherit;font-size:13px;color:var(--ink-2);background:transparent;border:1px solid var(--ring);
border-radius:8px;padding:6px 10px;cursor:pointer}
.ghost:hover{background:var(--hover)}
.hero{display:grid;grid-template-columns:1fr auto;gap:16px 32px;align-items:end}
.figure{font-size:64px;line-height:1;font-weight:650;letter-spacing:-.03em}
.figure-label{font-size:16px;margin-top:6px}
.figure-note{font-size:13px;color:var(--ink-2);margin-top:4px}
.hero-side{display:flex;gap:28px}
.stat{display:flex;flex-direction:column}
.stat-value{font-size:32px;font-weight:650;line-height:1.1}
.of{font-size:16px;color:var(--ink-2);font-weight:500}
.stat-label{font-size:13px;color:var(--ink-2)}
.attention{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:4px 24px;margin:0;padding:12px 0 0;list-style:none;
font-size:14px;color:var(--ink-2);border-top:1px solid var(--grid)}
.attention li{display:flex;align-items:baseline}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px;flex:none;
background:var(--muted);vertical-align:1px}
.dot.act{background:var(--act)}.dot.act-still{background:var(--act-still)}.dot.warn{background:var(--warn)}
.dot.bad{background:var(--bad)}.dot.muted{background:var(--axis)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin-top:10px}
.robot-card{background:var(--surface);border:1px solid var(--ring);border-radius:14px;padding:16px}
.state{font-size:14px;color:var(--ink-2);display:flex;align-items:baseline}
.util{display:flex;align-items:baseline;gap:8px;margin:10px 0 6px}
.util-value{font-size:28px;font-weight:650;line-height:1}
.util-label{font-size:13px;color:var(--ink-2)}
.line{font-size:13px;color:var(--ink-2);margin-top:6px;overflow-wrap:anywhere}
.k{color:var(--muted);margin-right:4px}
.thin{display:flex;gap:2px;height:8px;margin:4px 0 4px}
.thin .seg{border-radius:4px;min-width:2px}
.seg.act,.block.act,.swatch.act{background:var(--act)}
.seg.dead,.block.dead,.swatch.dead{background:var(--dead)}
.block.claimed,.swatch.claimed{background:var(--claimed)}
.block.idle,.swatch.idle{background:var(--idle)}
.block.act-still,.swatch.act-still{background:var(--act-still)}
.block.unknown,.swatch.unknown{background:repeating-linear-gradient(45deg,var(--act-unknown) 0 4px,var(--hatch) 4px 5px)}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12px;color:var(--ink-2);margin-bottom:10px}
.legend.small{margin:6px 0 0}
.key{display:inline-flex;align-items:center;gap:6px}
.swatch{display:inline-block;width:12px;height:12px;border-radius:3px}
.swatch.r-moving{background:var(--act)}.swatch.r-still{background:var(--act-still)}
.swatch.r-unknown{background:repeating-linear-gradient(45deg,var(--act-unknown) 0 3px,var(--hatch) 3px 4px)}
.swatch.r-dead{background:var(--dead)}.swatch.r-idle{background:var(--idle);box-shadow:inset 0 0 0 1px var(--ring)}
.swatch.r-fault{background:var(--bad)}
.swatch.flag-key{width:0;height:0;border-radius:0;border-left:5px solid transparent;border-right:5px solid transparent;
border-top:7px solid var(--warn)}
.ribbon{position:relative;padding-bottom:22px}
.lane{display:grid;grid-template-columns:96px 1fr;align-items:center;gap:10px;margin:2px 0}
.lane-name{font-size:13px;color:var(--ink-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lane-svg{width:100%;height:34px;display:block;overflow:visible}
.lane-svg rect{shape-rendering:crispEdges}
.r-moving{fill:var(--act)}.r-still{fill:var(--act-still)}.r-unknown{fill:var(--act-unknown)}
.r-dead{fill:var(--dead)}.r-idle{fill:var(--idle)}.r-fault{fill:var(--bad)}
.hatch{fill:url(#hatch);pointer-events:none}.defs{position:absolute}.hatch-line{fill:var(--hatch)}
.flag{fill:var(--warn)}
.now{stroke:var(--ink);stroke-width:1.5;vector-effect:non-scaling-stroke}
.axis{position:relative;height:18px;margin-left:106px;font-size:11px;color:var(--muted);
border-top:1px solid var(--axis)}
.axis span{position:absolute;top:3px;transform:translateX(-50%)}
.axis span:first-child{transform:none}.axis span:last-child{transform:translateX(-100%)}
.crosshair{position:absolute;top:0;bottom:22px;width:1px;background:var(--ink);opacity:.5;pointer-events:none}
.tip{position:fixed;z-index:10;max-width:340px;background:var(--raised);color:var(--ink);border:1px solid var(--ring);
border-radius:8px;padding:8px 10px;font-size:12px;line-height:1.45;box-shadow:0 6px 24px rgba(0,0,0,.14);
pointer-events:none;overflow-wrap:anywhere}
details.robot{border-top:1px solid var(--grid)}
details.robot:first-of-type{border-top:0}
details.robot>summary{list-style:none;display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 24px;
padding:12px 0 12px 20px;cursor:pointer;position:relative}
details.robot>summary::-webkit-details-marker{display:none}
details.robot>summary::before{content:"\\203A";position:absolute;left:2px;top:9px;font-size:20px;line-height:1;
color:var(--muted);transition:transform .15s}
details.robot[open]>summary::before{transform:rotate(90deg)}
.summary-head{display:inline-flex;gap:16px;align-items:baseline}
.summary-name{font-weight:650;min-width:84px}
.summary-util{color:var(--ink-2);font-size:14px}
.summary-reason{color:var(--ink-2);font-size:14px;display:inline-flex;align-items:baseline}
.robot-body{padding:4px 0 18px 20px}
.splits{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;margin-bottom:16px}
.split-name{font-size:13px;color:var(--ink-2);margin-bottom:6px}
.tall{display:flex;gap:2px;height:28px}
.tall .block{border-radius:4px;min-width:2px;display:flex;align-items:center;padding-left:8px;overflow:hidden}
.in{font-size:12px;font-weight:600;color:#fff;white-space:nowrap}
.block.idle .in,.block.dead .in,.block.act-still .in,.block.unknown .in{color:#0b0b0b}
.panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px 24px}
.panel{min-width:0;margin-top:14px}.panel.narrow{max-width:560px}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:500;color:var(--muted);padding:4px 8px 6px 0;border-bottom:1px solid var(--grid);
white-space:nowrap}
td{padding:6px 8px 6px 0;border-bottom:1px solid var(--grid);vertical-align:top}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.reason{overflow-wrap:anywhere}
.barcell{width:28%;min-width:60px}
.hbar{display:block;height:8px;border-radius:0 4px 4px 0;margin-top:5px;min-width:2px}
.hbar.warn{background:var(--warn)}.hbar.bad{background:var(--bad)}.hbar.act-still{background:var(--act-still)}
.exact{font-size:12px;color:var(--ink-2)}
.exact summary{cursor:pointer;color:var(--muted)}
.exact ul{margin:4px 0 0;padding-left:16px}
.when{white-space:nowrap}
.policy-link{text-decoration:none}.policy-link:hover code{text-decoration:underline}
.notes{list-style:none;margin:10px 0 0;padding:0;font-size:13px;color:var(--ink-2)}
.notes li{display:flex;align-items:baseline}
.id{font:11px/1.3 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted)}
.why div{display:flex;align-items:baseline}
.policy-cell{max-width:280px}
.flags,.actions{list-style:none;margin:0;padding:0}
.flag-row,.action{display:flex;gap:2px;padding:10px 0;border-top:1px solid var(--grid);font-size:14px}
.flag-row:first-child,.action:first-child{border-top:0}
.flag-row .dot,.action .dot{margin-top:7px}
.policy{margin-top:4px}
.message{color:var(--ink-2);font-size:13px;overflow-wrap:anywhere}
footer{margin-top:24px;font-size:13px;color:var(--ink-2);display:grid;gap:6px;max-width:80ch}
@media (max-width:640px){main{padding:16px 16px 40px}.hero{grid-template-columns:1fr}.figure{font-size:56px}
.hero-side{gap:24px}.lane{grid-template-columns:1fr;gap:0}.lane-name{font-size:12px}.axis{margin-left:0}
.panels{grid-template-columns:1fr}.robot-body{padding-left:0}.top{align-items:flex-start}
.policy-cell{max-width:none}
table.stack thead{display:none}
table.stack tr{display:flex;flex-direction:column;padding:10px 0;border-bottom:1px solid var(--grid)}
table.stack tr:last-child{border-bottom:0}
table.stack td{display:flex;gap:10px;border:0;padding:2px 0;text-align:left;white-space:normal}
table.stack td::before{content:attr(data-label);flex:0 0 112px;color:var(--muted)}
table.stack td.lead{font-weight:600;display:block;order:-1}table.stack td.lead::before{content:none}
table.stack td.lead .id{font-weight:400}}
@media print{.ghost{display:none}details.robot{break-inside:avoid}}
"""

SCRIPT = """
(function(){
  var tip=document.getElementById('tip');
  function show(text,x,y){tip.textContent=text;tip.hidden=false;var w=tip.offsetWidth,h=tip.offsetHeight;
    var left=Math.min(x+14,window.innerWidth-w-8),top=y+16+h>window.innerHeight?y-h-12:y+16;
    tip.style.left=Math.max(8,left)+'px';tip.style.top=Math.max(8,top)+'px';}
  function hide(){tip.hidden=true;}
  document.querySelectorAll('[data-ribbon]').forEach(function(ribbon){
    var cross=ribbon.querySelector('.crosshair');
    ribbon.addEventListener('pointermove',function(event){
      var svg=event.target.closest&&event.target.closest('svg');
      if(!svg){hide();cross.hidden=true;return;}
      var box=svg.getBoundingClientRect(),frac=(event.clientX-box.left)/box.width,vx=frac*1000,found=null;
      var marks=svg.querySelectorAll('[data-tip]');
      for(var i=0;i<marks.length;i++){var m=marks[i];if(m.tagName==='path'&&event.target===m){found=m;break;}
        if(m.tagName==='rect'){var x=+m.getAttribute('x'),w=+m.getAttribute('width');
          if(vx>=x&&vx<=x+w){found=m;}}}
      var rbox=ribbon.getBoundingClientRect();cross.hidden=false;cross.style.left=(event.clientX-rbox.left)+'px';
      if(found){show(found.getAttribute('data-tip'),event.clientX,event.clientY);}else{hide();}
    });
    ribbon.addEventListener('pointerleave',function(){hide();cross.hidden=true;});
  });
  var expand=document.querySelector('[data-expand]');
  if(expand){expand.addEventListener('click',function(){var all=document.querySelectorAll('details.robot');
    var open=Array.prototype.some.call(all,function(d){return !d.open;});
    all.forEach(function(d){d.open=open;});expand.textContent=open?'Collapse all':'Expand all';});}
  if(location.hash==='#expanded'){document.querySelectorAll('details.robot').forEach(function(d){d.open=true;});}
})();
"""
