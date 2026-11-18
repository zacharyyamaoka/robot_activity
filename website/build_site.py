"""Build the landing page (GitHub Pages) from real runs of the package on its demo fleet.

    uv run --extra site --with playwright python website/build_site.py     # writes website/_build/
    python3 website/publish_pages.py                                        # commits website/_build/ to gh-pages

Everything on the page is produced by the package, here, at build time:

- the hero video is the real dashboard (`robot_activity.views.html.dashboard(..., until=T)`) rendered every 36
  sim-minutes of the demo day and screenshotted by headless Chrome, then stitched with ffmpeg;
- the dashboard crops are screenshots of `dashboard.html`, which is published beside the page;
- the queue screen is the real `robot-activity queue` command, run on the demo's queue as its ledger stood at 12:15;
- the quickstart outputs come from running those commands verbatim in a clean, isolated environment;
- the W&B chart is drawn from `robot_activity.wandb_log.payload`, the exact dictionaries `push` logs;
- the real-robot table compares one lab's in-house monitor (the frozen oracle in `tests/data/`) with this package's
  numbers on the same rows, computed here when the rows are on this machine (they are not public).

The page is self-contained (inline CSS, a few lines of JS, no CDN), light and dark, and phone-width safe; the build
renders it at 1280 and 390 px, fails on horizontal overflow or a console error, and writes those screenshots to
`ROBOT_ACTIVITY_SITE_SHOTS` (default `reports/media/site/`, gitignored).
"""

from __future__ import annotations

import hashlib
import html
import importlib.util
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = Path(os.environ.get("ROBOT_ACTIVITY_SITE_OUT") or HERE / "_build")
SHOTS = Path(os.environ.get("ROBOT_ACTIVITY_SITE_SHOTS") or ROOT / "reports" / "media" / "site")
sys.path.insert(0, str(HERE))

from capture import Browser, encode_video
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import BashLexer, PythonLexer, TOMLLexer

from robot_activity.demo import simulate_fleet
from robot_activity.metrics import hours, owners, why_still
from robot_activity.queue import Queue, replay
from robot_activity.store import Store
from robot_activity.views.html import dashboard
from robot_activity.wandb_log import fleet_payload, payload

REPO = "https://github.com/zacharyyamaoka/robot_activity"
SITE = "https://zacharyyamaoka.github.io/robot_activity/"
REMIT = "Keep every robot doing useful work, and say why when it isn't."
DAY, TZ = "2026-11-18", "UTC"
FRAME_STEP_MIN = 36            # the hero video: one frame every 36 sim-minutes, 40 frames for the day
FRAME_FPS = 3.0
QUEUE_AT_H = 12.25             # the queue screen: the ledger as it stood at 12:15 on the demo day
FRAME_CLIP = {"x": 80, "y": 0, "width": 1120, "height": 1128}
PRIVATE = re.compile(r"/home/|/archive|/Users/|@gmail|\.netrc|wandb_api|api_key|gh[pousr]_[A-Za-z0-9]{20}|bam-GPU")
SERIES = (("arm-1", "s1"), ("arm-2", "s2"), ("pendulum-1", "s3"))   # categorical slots in fixed order
# WHY inline: a page without an icon makes every browser ask the host root for /favicon.ico (a console 404 here)
FAVICON = ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><rect width='16' "
           "height='16' rx='3' fill='%232a78d6'/><rect x='3' y='8' width='2.5' height='5' fill='white'/><rect x='6.75' "
           "y='5' width='2.5' height='8' fill='white'/><rect x='10.5' y='3' width='2.5' height='10' fill='white'/></svg>")

# The video folds the long middle of the dashboard away so the agent's latest actions stay in frame.
FRAME_CSS = """
section[aria-label="Robots"], section[aria-label="Stalemates"], section[aria-label="The active queue"] {display:none}
.ghost {visibility:hidden}
section[aria-label="The agent"] > p.hint, section[aria-label="The agent"] .panel.narrow,
section[aria-label="The agent"] ul.notes {display:none}
section[aria-label="The agent"] .actions li:nth-child(n+3) {display:none}
section[aria-label="The agent"] .actions + p.calm {display:none}
"""


# ---- the demo fleet ------------------------------------------------------------------------------------------------

def run_demo(work: Path) -> tuple[Store, Queue]:
    simulate_fleet(work / "fleet", day=DAY, tz=TZ)
    return Store(work / "fleet", tz=TZ), Queue(work / "fleet")


def queue_at(queue: Queue, until: float, root: Path) -> Path:
    """A queue folder holding the ledger up to `until` and every item as it stood then (via `replay`)."""
    events = [event for event in queue.events() if event["t"] <= until]
    items_dir = root / "queue" / "items"
    items_dir.mkdir(parents=True, exist_ok=True)
    for item in replay(events).values():
        (items_dir / f"{item.id}.json").write_text(json.dumps(item.to_dict(), indent=2) + "\n", encoding="utf-8")
    (root / "queue" / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events), "utf-8")
    return root


def check_replay_rebuild(queue: Queue, work: Path) -> None:
    """The rebuilt folder at the end of the ledger must equal the demo's own item files, byte for byte."""
    rebuilt = queue_at(queue, float("inf"), work / "rebuilt")
    for path in sorted(queue.items_dir.glob("*.json")):
        if path.read_bytes() != (rebuilt / "queue" / "items" / path.name).read_bytes():
            raise SystemExit(f"replay does not rebuild {path.name}: the 12:15 queue screen would not be faithful")


def queue_screen(queue: Queue, store: Store, work: Path) -> tuple[str, str]:
    """The real `robot-activity queue` command on the 12:15 queue, its clock pinned to 12:15."""
    until = store.day_bounds(DAY)[0] + QUEUE_AT_H * 3600
    root = queue_at(queue, until, work / "queue-1215")
    code = ("import sys, time; time.time = lambda: float(sys.argv[1]); from robot_activity.cli import main; "
            "raise SystemExit(main(['queue', '--root', sys.argv[2]]))")
    done = subprocess.run([sys.executable, "-c", code, repr(until), str(root)], capture_output=True, text=True,
                          check=True)
    return done.stdout, store.local(until).strftime("%H:%M")


# ---- the quickstart, run verbatim in a clean environment ----------------------------------------------------------

QUICKSTART = (
    "robot-activity demo --root fleet",
    "robot-activity summary --root fleet",
    "robot-activity dashboard --root fleet --out activity.html",
)


def run_quickstart(work: Path) -> list[tuple[str, str]]:
    """Each command in a fresh `uv run --isolated` environment with only this package installed (no project venv)."""
    folder = work / "quickstart"
    folder.mkdir()
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    env["UV_NO_PROGRESS"] = "1"
    env.pop("ROBOT_ACTIVITY_TZ", None)   # WHY: verbatim means the reader's defaults, no hidden flags
    results = []
    for command in QUICKSTART:
        argv = ["uv", "run", "-q", "--isolated", "--no-project", "--with", str(ROOT), *command.split()]
        done = subprocess.run(argv, cwd=folder, env=env, capture_output=True, text=True, check=True)
        results.append((command, done.stdout))
    return results


# ---- the real day (one lab's in-house monitor vs this package) -----------------------------------------------------

def real_parity() -> dict:
    oracle = json.loads((ROOT / "tests" / "data" / "bam_fb_v2-2026-11-17.oracle.json").read_text())
    rows_path = Path(os.environ.get("ROBOT_ACTIVITY_BAM_ROWS", oracle["input"]))
    result = {"oracle": oracle, "ours": None}
    if not rows_path.is_file():
        return result
    data = rows_path.read_bytes()
    if hashlib.sha256(data).hexdigest() != oracle["input_sha256"]:
        return result
    spec = importlib.util.spec_from_file_location("bam_rows", ROOT / "examples" / "bam_rows.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = []
    for line in data.decode().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    samples = module.to_samples(sorted(rows, key=lambda row: row["t"]))
    ours = {"hours": asdict(hours(samples)), "owners": [asdict(entry) for entry in owners(samples)],
            "why_still": [asdict(entry) for entry in why_still(samples)]}
    worst = max([abs(ours["hours"][name] - value) for name, value in oracle["hours"].items()]
                + [abs(mine["hours"] - theirs["hours"]) for mine, theirs in zip(ours["why_still"], oracle["why_still"])]
                + [abs(mine[key] - theirs[key]) for mine, theirs in zip(ours["owners"], oracle["owners"])
                   for key in ("hours", "moving_h", "still_h", "unknown_h")])
    if worst > oracle["tolerance_h"]:
        raise SystemExit(f"real-day parity broke: worst difference {worst:.4f} h > {oracle['tolerance_h']} h")
    result.update(ours=ours, worst=worst)
    return result


# ---- captures ------------------------------------------------------------------------------------------------------

def record_hero(browser: Browser, store: Store, queue: Queue, work: Path) -> None:
    frames_dir = work / "frames"
    frames_dir.mkdir()
    day_start, day_end = store.day_bounds(DAY)
    frames = []
    step = FRAME_STEP_MIN * 60
    times = [day_start + step * index for index in range(1, int((day_end - day_start) // step) + 1)]
    times[-1] = min(times[-1], day_end - 10)   # the last row of the day, so the clock reads 23:59, not 00:00
    for index, until in enumerate(times):
        page = dashboard(store, queue, DAY, frames_dir / f"frame-{index:03d}.html", until=until)
        frames.append(browser.shot(page.resolve().as_uri(), frames_dir / f"frame-{index:03d}.png", width=1280,
                                   height=FRAME_CLIP["height"], clip=FRAME_CLIP, css=FRAME_CSS))
    media = OUT / "media"
    media.mkdir(parents=True, exist_ok=True)
    encode_video(frames, media / "hero.mp4", media / "hero.gif", fps=FRAME_FPS, hold_last_s=2.0, gif_width=560)
    shutil.copyfile(frames[-1], media / "hero-poster.png")


def capture_crops(browser: Browser) -> None:
    img = OUT / "img"
    img.mkdir(parents=True, exist_ok=True)
    url = (OUT / "dashboard.html").resolve().as_uri() + "#expanded"
    browser.element(url, "details.robot", img / "robot-arm-2.png", nth=1, pad=8)
    browser.element(url, "section[aria-label='Stalemates']", img / "stalemates.png")
    browser.element(url, "section[aria-label='The agent']", img / "agent.png",
                    css="section[aria-label='The agent'] .actions li:nth-child(n+3){display:none}"
                        " section[aria-label='The agent'] .actions + p.calm{display:none}")
    browser.element(url, "section[aria-label='The active queue']", img / "queue-dashboard.png")
    browser.shot(url, img / "dashboard-phone.png", width=390, height=844)


# ---- rendering helpers ---------------------------------------------------------------------------------------------

FORMATTER = HtmlFormatter(nowrap=True)


def code(text: str, lexer) -> str:
    return f"<pre class=\"src\">{highlight(text, lexer, FORMATTER).rstrip()}</pre>"


def bash(text: str) -> str:
    return code(text, BashLexer())


def output(text: str) -> str:
    return f"<pre class=\"out\">{esc(text.rstrip())}</pre>"


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def h(value: float) -> str:
    return f"{value:.2f} h"


def pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def figure(src: str, alt: str, caption: str, width: int, height: int, *, link: str | None = None) -> str:
    image = f"<img src=\"{src}\" alt=\"{esc(alt)}\" width=\"{width}\" height=\"{height}\" loading=\"lazy\">"
    if link:
        image = f"<a href=\"{link}\">{image}</a>"
    return f"<figure>{image}<figcaption>{caption}</figcaption></figure>"


def image_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")   # PNG IHDR


# ---- sections ------------------------------------------------------------------------------------------------------

def splits_svg(split) -> str:
    """The three splits of the demo fleet's 72 tracked hours, one bar each (real totals)."""
    width, bar_h, left = 420.0, 26.0, 0.0
    rows = [
        ("Claim", [("claimed", split.claimed_h, "c-claimed"), ("unclaimed", split.unclaimed_h, "c-idle")]),
        ("Activity", [("activity", split.activity_h, "c-act"), ("dead", split.dead_h, "c-dead")]),
        ("Motion", [("moving", split.moving_h, "c-act"), ("still", split.still_h, "c-still"),
                    ("unknown", split.unknown_h, "c-unknown")]),
    ]
    parts, y = [], 0.0
    for name, sides in rows:
        parts.append(f"<text class=\"sv-name\" x=\"0\" y=\"{y + 14:.0f}\">{name}</text>")
        x = left
        for label, value, cls in sides:
            w = value / split.tracked_h * width
            gap = 2.0 if x > left else 0.0
            parts.append(f"<rect class=\"{cls}\" x=\"{x + gap:.1f}\" y=\"{y + 22:.0f}\" width=\"{max(w - gap, 2):.1f}\" "
                         f"height=\"{bar_h:.0f}\" rx=\"4\"><title>{label}: {value:.1f} h</title></rect>")
            if cls == "c-unknown":
                parts.append(f"<rect class=\"hatch\" x=\"{x + gap:.1f}\" y=\"{y + 22:.0f}\" "
                             f"width=\"{max(w - gap, 2):.1f}\" height=\"{bar_h:.0f}\" rx=\"4\"></rect>")
            x += w
        legend = " · ".join(f"{label} {value:.1f} h" for label, value, _ in sides)
        parts.append(f"<text class=\"sv-legend\" x=\"0\" y=\"{y + 66:.0f}\">{legend}</text>")
        y += 88
    defs = ("<defs><pattern id=\"sv-hatch\" width=\"5\" height=\"5\" patternUnits=\"userSpaceOnUse\" "
            "patternTransform=\"rotate(45)\"><rect class=\"hatch-line\" width=\"1.4\" height=\"5\"></rect></pattern></defs>")
    return (f"<svg class=\"splits-svg\" viewBox=\"0 0 {width:.0f} {y - 12:.0f}\" role=\"img\" aria-label=\"The demo "
            f"fleet's {split.tracked_h:.0f} tracked hours on the three splits\">{defs}{''.join(parts)}</svg>")


def wandb_chart(store: Store, queue: Queue) -> tuple[str, list[dict]]:
    """Activity share through the day, one strip per robot (small multiples, so no line hides another), from the
    per-minute steps `push` logs, as a 30-minute rolling mean."""
    day_start, _ = store.day_bounds(DAY)
    series, runs = [], []
    for robot, slot in SERIES:
        run = payload(store, queue, robot, DAY)
        runs.append(run)
        by_minute = {int((step["minute_t"] - day_start) // 60): step["activity_frac"] for step in run["steps"]}
        points = []
        for minute in range(0, 1441, 5):
            window = [by_minute[m] for m in range(minute - 29, minute + 1) if m in by_minute]
            points.append((minute / 60.0, statistics.fmean(window) if window else 0.0))
        series.append((robot, slot, points, run["summary"]["utilization"]))
    width, height = 1000.0, 60.0
    strips = []
    for robot, slot, points, utilization in series:
        line = " ".join(f"{hour / 24 * width:.1f},{(1 - share) * height:.1f}" for hour, share in points)
        area = f"0,{height:.0f} {line} {width:.0f},{height:.0f}"
        strips.append(
            f"<div class=\"strip\"><div class=\"strip-name\"><b>{robot}</b><span>{pct(utilization)} today</span></div>"
            f"<div class=\"strip-plot\"><svg viewBox=\"0 0 {width:.0f} {height:.0f}\" preserveAspectRatio=\"none\" "
            f"role=\"img\" aria-label=\"{robot}: activity share through the day\">"
            f"<line class=\"grid\" x1=\"0\" x2=\"{width:.0f}\" y1=\"{height / 2:.0f}\" y2=\"{height / 2:.0f}\"></line>"
            f"<polygon class=\"area {slot}\" points=\"{area}\"></polygon>"
            f"<polyline class=\"line {slot}\" points=\"{line}\"></polyline>"
            f"<line class=\"axis-line\" x1=\"0\" x2=\"{width:.0f}\" y1=\"{height:.0f}\" y2=\"{height:.0f}\"></line>"
            "</svg></div></div>")
    data = json.dumps({robot: [round(share, 3) for _, share in points] for robot, _, points, _ in series})
    x_labels = "".join(f"<span style=\"left:{hour / 24 * 100:.2f}%\">{hour:02d}:00</span>" for hour in range(0, 25, 6))
    chart = (f"<div class=\"chart\" data-chart='{esc(data)}'><div class=\"strips\">{''.join(strips)}"
             "<div class=\"cross\" hidden></div><div class=\"tip\" hidden></div></div>"
             f"<div class=\"xlab\">{x_labels}</div></div>")
    return chart, runs


def remit_section() -> str:
    does = [
        "<b>Owns the activity numbers</b>, per robot and for the fleet, out of every tracked hour.",
        "<b>Says why</b> every dead or still hour happened: a fault, blocked on code, queue waiting, nothing "
        "dispatched, unclaimed.",
        "<b>Keeps a full, moving queue</b>: prompts the executor of the next runnable item, asks owners for ETAs, "
        "enqueues the program's own declared fillers.",
        "<b>Flags stalemates</b>: stuck, looping between the same two poses, or past its estimate, with the policy "
        "to read.",
    ]
    never = [
        "<b>Decides what the work is.</b> Every item belongs to its owner: an agent, a person, or the deployed "
        "system.",
        "<b>Starts, finishes, blocks or edits</b> anyone's item, or writes job content.",
        "<b>Optimises hours moving for its own sake.</b> It is a proxy; inventing motion is the stalemate it exists "
        "to catch.",
        "<b>Investigates a fault itself.</b> It routes it to the owner and the dispatcher, with what it saw.",
    ]
    return (
        "<section id=\"remit\"><h2>What it does, and never does</h2>"
        "<p class=\"lede-s\">A member of the team with one remit, written as a role prompt any agent harness can load "
        f"(<a href=\"{REPO}/blob/main/AGENT.md\"><code>AGENT.md</code></a>).</p>"
        "<div class=\"two\"><div class=\"panel\"><h3>Does</h3><ul class=\"ticks\">"
        + "".join(f"<li>{item}</li>" for item in does)
        + "</ul></div><div class=\"panel\"><h3>Never does</h3><ul class=\"crosses\">"
        + "".join(f"<li>{item}</li>" for item in never)
        + "</ul></div></div>"
        "<h3>Its loop, every tick, per robot</h3><ol class=\"loop\">"
        "<li><b>Dead for 3 min or more?</b> A runnable item waits: prompt its executor, naming the item, its estimate "
        "and its policy. Nothing runnable: ask the owners of the top not-ready items for an ETA, with each item's "
        "reasons, once per answer.</li>"
        "<li><b>Queue low?</b> Enqueue the program's declared fillers (recalibrate, self-test, a hard task to "
        "practise), then ask for ETAs if it is still low.</li>"
        "<li><b>Stalemate?</b> Tell the item's owner and the dispatcher, with the shared policy to read.</li></ol>"
        "<p>Its own KPI is <b>idle unprompted</b>: dead time past 3 minutes with nobody prompted. The target is zero, "
        "and it is the agent's alone.</p></section>")


def splits_section(split) -> str:
    rules = [
        ("Claim", "claimed / unclaimed", "Someone (<code>claimed_by</code>) holds the robot."),
        ("Activity", "activity / dead", "Claimed, a queue item dispatched, and health <code>ok</code>. Being still "
         "inside a running item is still activity."),
        ("Motion", "moving / still / unknown", "From the joints: any joint's range above 0.3° in the window. "
         "<b>Unknown</b> (no joint feed) is never counted as idle."),
    ]
    items = "".join(f"<li><b>{name}</b> <span class=\"muted\">{sides}</span><br>{rule}</li>" for name, sides, rule in rules)
    return (
        "<section id=\"splits\"><h2>Three clean splits of tracked time</h2>"
        "<p class=\"lede-s\">Every tracked second of every robot falls on exactly one side of each split. Tracked time "
        "is the sum of the rows' windows; a gap with no rows (the sampler was down) is untracked, never idle. "
        "<b>Utilisation = activity ÷ all tracked hours.</b></p>"
        f"<div class=\"splits\"><div>{splits_svg(split)}<p class=\"caption\">The demo fleet's day: three robots, "
        f"{split.tracked_h:.0f} tracked hours, {pct(split.utilization)} utilisation.</p></div>"
        f"<ul class=\"rules\">{items}</ul></div></section>")


def metrics_section(store: Store, rows_by_robot: dict) -> str:
    tiles = []
    for robot, rows in rows_by_robot.items():
        split = hours(rows)
        tiles.append(f"<div class=\"tile\"><b>{pct(split.utilization)}</b><span>{robot} active</span>"
                     f"<span class=\"muted\">{split.activity_h:.1f} of {split.tracked_h:.0f} h · moving "
                     f"{pct(split.moving_share)}</span></div>")
    fleet = hours([row for rows in rows_by_robot.values() for row in rows])
    tiles.insert(0, f"<div class=\"tile lead\"><b>{pct(fleet.utilization)}</b><span>fleet utilisation</span>"
                    f"<span class=\"muted\">{fleet.activity_h:.1f} of {fleet.tracked_h:.0f} tracked h</span></div>")
    arm2 = image_size(OUT / "img" / "robot-arm-2.png")
    stale = image_size(OUT / "img" / "stalemates.png")
    agent = image_size(OUT / "img" / "agent.png")
    return (
        "<section id=\"metrics\"><p class=\"eyebrow\">Tool 1</p><h2>Metrics: where every hour went, and why</h2>"
        "<p class=\"lede-s\">One self-contained HTML dashboard per day (no network, light and dark, phone-width safe), "
        "a one-line terminal summary, and the same numbers as JSON. Utilisation per robot, the three splits, why "
        "each robot stood still, who held it, the longest dead streaks, the hand-off gap from one item to the next, "
        f"and stalemate flags. <a href=\"dashboard.html\">Open the demo dashboard</a>.</p>"
        f"<div class=\"tiles\">{''.join(tiles)}</div>"
        + figure("img/robot-arm-2.png", "arm-2's splits, why it stood still, why it was dead, who held it, dead "
                 "streaks and hand-off gaps", "<b>arm-2 on the demo day.</b> The three splits as bars, then "
                 "<i>Why it stood still</i> and <i>Who held the robot</i>, the longest dead streaks and the hand-off "
                 "gaps. Reasons that differ only in a count are grouped, their exact labels one click away.",
                 *arm2, link="dashboard.html#expanded")
        + figure("img/stalemates.png", "Stalemate flags: a looping regrasp, an overrun and a stuck run",
                 "<b>Stalemates.</b> Motion that looks like work: arm-1 looping between two poses, an overrun, a "
                 "frozen run on arm-2. Each flag carries the item's shared policy, so whoever reads it can check the "
                 "code.", *stale)
        + figure("img/agent.png", "The agent's KPI table and its latest actions",
                 "<b>The agent.</b> Its KPI, idle unprompted, per robot, and its latest actions. arm-1 declares no "
                 "filler, so when its queue empties the agent has nobody to ask, and the KPI shows exactly that.",
                 *agent)
        + "</section>")


def queue_section(screen: str, at: str) -> str:
    verbs = [
        ("queue", "the active queue on one screen (<code>--json</code>, <code>--watch</code>)"),
        ("queue submit", "a new item: title, owner, estimate, robot, and the policy it will run"),
        ("queue policy", "share the code an item will run (repo @ commit : path)"),
        ("queue block · unblock", "say why it cannot run yet (code, dependency, human, hardware, data) and clear it"),
        ("queue eta", "the owner's ETA for ready"),
        ("queue start · finish · cancel", "the executor starts it on a robot; it finishes ok or failed"),
    ]
    rows = "".join(f"<tr><td><code>robot-activity {verb}</code></td><td>{what}</td></tr>" for verb, what in verbs)
    queue_img = image_size(OUT / "img" / "queue-dashboard.png")
    return (
        "<section id=\"queue\"><p class=\"eyebrow\">Tool 2</p><h2>The queue, and a view of the active queue</h2>"
        "<p class=\"lede-s\">The queue is files a human can read: one JSON file per item, an append-only events "
        "ledger and a lock. Programs that never import the package use the CLI. This is the real "
        f"<code>robot-activity queue</code> on the demo fleet as its ledger stood at {at}:</p>"
        + bash("robot-activity queue --root fleet") + output(screen)
        + "<h3>The rules</h3><ul class=\"rules\">"
        "<li><b>Owner and executor.</b> Every item has an owner, who decides what it is, and an executor, who starts "
        "it (the owner by default).</li>"
        "<li><b>Ready means the code is shared.</b> An item is ready only once its owner attaches a "
        "<code>PolicyRef</code> (<code>repo@commit:path</code>), so a dispatcher that sees a stalemate can read the "
        "exact code and judge whether the behaviour is expected. A not-ready item always says why, with an ETA when "
        "known.</li>"
        "<li><b>Priority.</b> A deployed system's own work first (<code>system</code> 0), then agent work "
        "(<code>agent</code> 5), then the program's fillers (<code>filler</code> 9); first submitted runs first.</li>"
        "<li><b>One running item per robot.</b></li>"
        "<li><b>The events ledger.</b> Every change is one line with <code>by</code>, in a frozen shape "
        "(<code>robot-activity-event/1</code>). Replaying it rebuilds the queue at any moment: that is how this "
        "screen and the dashboard's replay were made.</li></ul>"
        f"<h3>The verbs</h3><table class=\"verbs\">{rows}</table>"
        + bash("robot-activity queue submit --root fleet --title \"pick vial\" --owner pnp-agent --est-min 6 --robot "
               "arm-1 --policy-repo https://github.com/you/policies --policy-commit 0123abcd --policy-path "
               "policies/pick.py")
        + bash("robot-activity queue block 0007-pick-vial --root fleet --kind code --why \"IK refuses the bin pose\" "
               "--eta +90m --by ik-agent")
        + figure("img/queue-dashboard.png", "The dashboard's active queue: running, runnable in start order, not "
                 "ready with why", "<b>The same queue on the dashboard</b> at the end of the demo day: running "
                 "now, runnable in start order, and every not-ready item with its why, owner, ETA and policy.",
                 *queue_img)
        + "</section>")


def wandb_section(chart: str, runs: list[dict], fleet_run: dict) -> str:
    rows = "".join(
        f"<tr><td><code>{run['run_id']}</code></td><td class=\"num\">{run['summary']['tracked_h']:.1f}</td>"
        f"<td class=\"num\">{run['summary']['activity_h']:.2f}</td><td class=\"num\">{run['summary']['moving_h']:.2f}</td>"
        f"<td class=\"num\">{pct(run['summary']['utilization'])}</td><td class=\"num\">{len(run['steps'])}</td></tr>"
        for run in runs)
    fleet = fleet_run["summary"]
    rows += (f"<tr><td><code>{fleet_run['run_id']}</code></td><td class=\"num\">{fleet['tracked_h']:.1f}</td>"
             f"<td class=\"num\">{fleet['activity_h']:.2f}</td><td class=\"num\">{fleet['moving_h']:.2f}</td>"
             f"<td class=\"num\">{pct(fleet['utilization'])}</td><td class=\"num\">{len(fleet_run['steps'])}</td></tr>")
    step_fields = ", ".join(f"<code>{key}</code>" for key in runs[0]["steps"][0] if key != "minute_t")
    return (
        "<section id=\"wandb\"><p class=\"eyebrow\">Tool 3</p><h2>Weights &amp; Biases tracking</h2>"
        "<p class=\"lede-s\">One command copies a day to W&amp;B: one run per robot per day and a fleet run, one step "
        "per minute, the day's hours as the run summary, and the why-still, owner, streak and hand-off tables. "
        "Pushing again resumes each run by id and logs only new minutes. W&amp;B is a copy of the local files, "
        "never the source of truth.</p>"
        + bash("pip install \"robot-activity[wandb] @ git+https://github.com/zacharyyamaoka/robot_activity\"")
        + bash("robot-activity wandb push --root fleet --project my-fleet")
        + bash("robot-activity wandb report --project my-fleet")
        + "<h3>What a push logs: the demo day</h3>"
        "<p class=\"caption\">Activity share per robot through the day (30-minute rolling mean of the "
        "per-minute <code>activity_frac</code>), drawn from <code>robot_activity.wandb_log.payload()</code>: the exact "
        "dictionaries <code>push</code> hands W&amp;B. Hover for values.</p>"
        f"{chart}"
        "<div class=\"scroll\"><table><thead><tr><th>run</th><th class=\"num\">tracked h</th><th class=\"num\">activity h"
        "</th><th class=\"num\">moving h</th><th class=\"num\">utilisation</th><th class=\"num\">steps</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
        f"<p class=\"caption\">Each step carries {step_fields}. <code>wandb report</code> saves a W&amp;B Report of "
        "the headline panels: utilisation and moving share per robot over the day, each run's totals, why it stood "
        "still and who held the robot. The demo's own W&amp;B project is private, so this page draws the same "
        "payload instead of linking it; its four run summaries equal these hours (checked through the W&amp;B API "
        "on 2026-11-18). Push your fleet and the same panels appear in your workspace.</p></section>")


def dropin_section(quickstart: list[tuple[str, str]]) -> str:
    steps = "".join(bash(command) + (output(text) if text.strip() else "") for command, text in quickstart)
    probe = ("from robot_activity.sampler import Observation\n\n\n"
             "def probe() -> Observation:\n"
             "    return Observation(claimed_by=current_holder(), joints=latest_joint_state())  "
             "# a robot_msgs JointState or a dict\n")
    filler = ('[[filler]]\ntitle = "self-test"\nowner = "rig-owner"\nest_min = 4.0\nrobot = "arm-1"\n'
              '[filler.policy]\nrepo = "https://github.com/example/rig-tools"\n'
              'commit = "0123456789abcdef0123456789abcdef01234567"\npath = "checks/self_test.py"\nentrypoint = "main"\n')
    return (
        "<section id=\"dropin\"><h2>Drop it into your program</h2>"
        "<p class=\"lede-s\">Runtime dependencies: <a href=\"https://pypi.org/project/robot-msgs/\">robot-msgs</a> and "
        "the standard library. <code>wandb</code> is an extra.</p>"
        "<h3>30-second quickstart</h3>"
        + bash("pip install \"robot-activity @ git+https://github.com/zacharyyamaoka/robot_activity\"")
        + steps
        + "<p class=\"caption\">Real output: each command run verbatim in a clean, isolated environment holding only "
        "this package. <code>demo</code> simulates today for two arms and a 1-DOF pendulum, their owners, a work queue "
        "and the real agent ticking every minute, so <code>summary</code> reads the day as it stands now.</p>"
        "<h3>Feed it your robot</h3>"
        "<p>The only program-specific part is a probe: who holds the robot, what item runs, its health and its joints, "
        "right now. Motion is any joint's range above 0.3° over the window; when the probe names no item, the queue's "
        "running item fills in.</p>"
        + code(probe, PythonLexer())
        + bash("robot-activity sample --root fleet --robot arm-1 --probe my_lab.activity:probe --every 10")
        + "<p>Already have a sampler? Map its rows onto <code>Sample</code> instead, as "
        f"<a href=\"{REPO}/blob/main/examples/bam_rows.py\"><code>examples/bam_rows.py</code></a> does for one lab's "
        "in-house monitor.</p>"
        "<h3>Declare your fillers</h3>"
        "<p>Fillers are the program's own fallback work. The agent only enqueues them, after every system and agent "
        f"item; it never invents work. Thresholds and fillers live in <a href=\"{REPO}/blob/main/examples/agent.toml\">"
        "<code>agent.toml</code></a>:</p>"
        + code(filler, TOMLLexer())
        + bash("robot-activity agent run --root fleet --config agent.toml --every 15")
        + "<div class=\"two\"><div><h3>For coding agents</h3><ul class=\"rules\">"
        f"<li><a href=\"{REPO}/blob/main/AGENT.md\"><code>AGENT.md</code></a>: the remit as a role prompt any agent "
        "harness can load.</li>"
        f"<li><a href=\"{REPO}/blob/main/integrations/claude_code/robot-activity-agent.md\">"
        "<code>robot-activity-agent.md</code></a>: a Claude Code subagent. Copy it into <code>.claude/agents/</code> "
        "and set <code>ROBOT_ACTIVITY_ROOT</code>.</li></ul></div>"
        "<div><h3>Where its prompts go</h3><ul class=\"rules\">"
        "<li>Every action is appended to <code>agent/actions.jsonl</code> and handed to a notifier.</li>"
        "<li><code>Outbox</code> (<code>--outbox FILE</code>): JSON lines for a reader of your own.</li>"
        "<li><code>CommandNotifier</code> (<code>--notify-cmd \"notify.sh\"</code>): runs any command with the "
        "action's JSON on stdin: a Slack webhook script, <code>claude -p</code>, an agent's inbox.</li>"
        "<li>The same prompt goes out at most once every 15 minutes.</li></ul></div></div></section>")


def real_section(parity: dict) -> str:
    oracle, ours = parity["oracle"], parity["ours"]
    names = (("tracked_h", "tracked"), ("claimed_h", "claimed"), ("unclaimed_h", "unclaimed"),
             ("activity_h", "activity (in a job)"), ("moving_h", "moving"), ("still_h", "still"),
             ("unknown_h", "unknown (no joint feed)"))

    def row(label: str, theirs: float, mine: float | None) -> str:
        if mine is None:
            return f"<tr><td>{label}</td><td class=\"num\">{h(theirs)}</td><td class=\"num\">—</td><td></td></tr>"
        return (f"<tr><td>{label}</td><td class=\"num\">{h(theirs)}</td><td class=\"num\">{h(mine)}</td>"
                f"<td class=\"num\">{abs(mine - theirs):.3f}</td></tr>")

    hour_rows = "".join(row(label, oracle["hours"][key], ours["hours"][key] if ours else None) for key, label in names)
    owner_rows = "".join(row(entry["owner"], entry["hours"], ours["owners"][index]["hours"] if ours else None)
                         for index, entry in enumerate(oracle["owners"]))
    still_rows = "".join(row(entry["reason"], entry["hours"], ours["why_still"][index]["hours"] if ours else None)
                         for index, entry in enumerate(oracle["why_still"][:4]))
    head = ("<thead><tr><th></th><th class=\"num\">their monitor</th><th class=\"num\">robot_activity</th>"
            "<th class=\"num\">|Δ| h</th></tr></thead>")
    verdict = (f"Worst difference {parity['worst']:.3f} h, inside the 0.01 h the in-house rounding allows."
               if ours else "This package's column is computed where the rows are; the test pins it.")
    return (
        "<section id=\"real-robot\"><h2>On a real robot</h2>"
        "<p class=\"lede-s\">Before this package, one robotics lab already ran its own activity monitor on a real arm. "
        "An adapter maps that monitor's rows onto <code>Sample</code>, and this package's numbers reproduce the "
        f"monitor's own roll-up for {oracle['day']} (one row per 10 s, {oracle['input_rows']:,} rows). Aggregates only; "
        f"the rows themselves stay in the lab. {verdict}</p>"
        f"<div class=\"two\"><div><h3>Hours</h3><div class=\"scroll\"><table>{head}<tbody>{hour_rows}</tbody></table>"
        f"</div></div><div><h3>Who held the robot</h3><div class=\"scroll\"><table>{head}<tbody>{owner_rows}</tbody>"
        f"</table></div><h3>Why it stood still (top 4)</h3><div class=\"scroll\"><table>{head}<tbody>"
        f"{still_rows}</tbody></table></div></div></div>"
        "<p class=\"caption\">The in-house figures are that monitor's own code, frozen in "
        f"<a href=\"{REPO}/blob/main/tests/data/bam_fb_v2-2026-11-17.oracle.json\"><code>tests/data/</code></a>; "
        f"<a href=\"{REPO}/blob/main/tests/test_real_parity.py\"><code>tests/test_real_parity.py</code></a> requires "
        "agreement within 0.01 h and skips cleanly where the rows are absent. The robot spent 19.3 of 24 hours "
        "still, 9.5 of them blocked on code: the kind of answer the agent exists to give.</p></section>")


def shoulders_section() -> str:
    entries = [
        ("OEE / TEEP availability", "https://www.oee.com/teep/",
         "Utilisation here is activity ÷ <i>all</i> tracked hours: TEEP-style availability over calendar time, not "
         "just scheduled time. It is the number a plant manager watches on an idle press."),
        ("PackML machine states", "https://www.omac.org/packml",
         "<i>Execute</i> is activity; <i>Idle</i>, <i>Starved</i>, <i>Held</i> and <i>Aborted</i> are dead reasons. "
         "The reason ladder is a small, explicit state model in the same spirit."),
        ("Theory of Constraints", "https://www.tocinstitute.org/theory-of-constraints.html",
         "Some idle time is slack a bottleneck needs. Hours moving is never the objective: pair it with a throughput "
         "KPI such as useful picks per day."),
        ("Goodhart's law", "https://en.wikipedia.org/wiki/Goodhart%27s_law",
         "A measure that becomes a target stops measuring. A robot can be made to move without doing anything useful; "
         "the stalemate rules guard against motion that only looks like work."),
        ("TriFinger and the Real Robot Challenge", "https://arxiv.org/abs/2308.07741",
         "Real robots in a shared lab, jobs submitted as code and run from a queue, with the "
         "<a href=\"https://arxiv.org/abs/2008.03596\">open TriFinger platform</a> underneath: the submission-queue "
         "pattern this package's queue follows."),
        ("AutoEval", "https://auto-eval.github.io/",
         "Policies queued for evaluation on real arms around the clock, with automatic resets: robots kept doing "
         "useful work without a person in the loop (<a href=\"https://arxiv.org/abs/2503.24278\">paper</a>)."),
    ]
    cards = "".join(f"<div class=\"panel\"><h3><a href=\"{url}\">{name}</a></h3><p>{text}</p></div>"
                    for name, url, text in entries)
    return ("<section id=\"shoulders\"><h2>Standing on shoulders</h2>"
            "<p class=\"lede-s\">Nothing here is new. It is plant-floor practice and shared-robot-lab practice, made "
            f"small enough to drop in.</p><div class=\"grid3\">{cards}</div></section>")


# ---- the page ------------------------------------------------------------------------------------------------------

CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--raised:#fff;--ink:#0b0b0b;--ink-2:#52514e;--muted:#6f6e69;
--line:#e1e0d9;--axis:#c3c2b7;--ring:rgba(11,11,11,.10);--code:#f1f0ec;--accent:#2a78d6;--act:#2a78d6;--still:#86b6ef;
--unknown:#cde2fb;--hatch:#6da7ec;--claimed:#52514e;--dead:#c3c2b7;--idle:#e6e5df;--s1:#2a78d6;--s2:#eb6834;
--s3:#1baf7a;--ok:#1baf7a;--no:#d03b3b;--tok-k:#7a3fb8;--tok-s:#1d7a4f;--tok-c:#898781;--tok-n:#2a62b8;--tok-o:#52514e}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#0d0d0d;--surface:#161615;
--raised:#20201f;--ink:#f0efec;--ink-2:#c3c2b7;--muted:#9a998f;--line:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);
--code:#1d1d1c;--accent:#5598e7;--act:#3987e5;--still:#1c5cab;--unknown:#13345e;--hatch:#3987e5;--claimed:#c3c2b7;
--dead:#52514e;--idle:#2c2c2a;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--tok-k:#c49af0;--tok-s:#7fd1a3;--tok-c:#8a897f;
--tok-n:#8fbcf2;--tok-o:#c3c2b7}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#161615;--raised:#20201f;--ink:#f0efec;
--ink-2:#c3c2b7;--muted:#9a998f;--line:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);--code:#1d1d1c;
--accent:#5598e7;--act:#3987e5;--still:#1c5cab;--unknown:#13345e;--hatch:#3987e5;--claimed:#c3c2b7;--dead:#52514e;
--idle:#2c2c2a;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--tok-k:#c49af0;--tok-s:#7fd1a3;--tok-c:#8a897f;--tok-n:#8fbcf2;
--tok-o:#c3c2b7}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{margin:0;background:var(--page);color:var(--ink);font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1040px;margin:0 auto;padding:0 16px 40px}
a{color:var(--accent)}
h1{font-size:44px;line-height:1.1;margin:0 0 10px;letter-spacing:-.02em;font-weight:700}
h2{font-size:28px;line-height:1.2;margin:0 0 8px;letter-spacing:-.01em}
h3{font-size:17px;margin:22px 0 8px}
p{margin:0 0 12px}
section{padding:44px 0 8px;border-top:1px solid var(--line);margin-top:28px}
.hero{padding:48px 0 0;border:0;margin:0}
.eyebrow{color:var(--accent);font-size:13px;font-weight:650;letter-spacing:.06em;text-transform:uppercase;margin:0 0 6px}
.remit{font-size:24px;line-height:1.35;font-weight:600;margin:0 0 12px;max-width:30ch}
.lede{font-size:18px;color:var(--ink-2);max-width:68ch}
.lede-s{color:var(--ink-2);max-width:72ch}
.muted{color:var(--muted)}
.cta{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0 26px}
.btn{display:inline-block;text-decoration:none;font-weight:600;font-size:15px;padding:9px 16px;border-radius:10px;
border:1px solid var(--ring);color:var(--ink);background:var(--surface)}
.btn.primary{background:var(--ink);color:var(--page);border-color:var(--ink)}
.video{max-width:900px;border:1px solid var(--ring);border-radius:14px;overflow:hidden;background:var(--surface);
box-shadow:0 10px 40px rgba(0,0,0,.08)}
.video video,.video img{display:block;width:100%;height:auto}
.caption,figcaption{color:var(--ink-2);font-size:14px;margin:8px 0 0}
.glance{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-top:28px}
.card{display:block;text-decoration:none;color:inherit;background:var(--surface);border:1px solid var(--ring);
border-radius:14px;padding:16px 18px}
.card:hover{border-color:var(--accent)}
.card b{display:block;font-size:13px;color:var(--accent);text-transform:uppercase;letter-spacing:.05em}
.card strong{display:block;font-size:18px;margin:2px 0 4px}
.card span{color:var(--ink-2);font-size:14.5px}
.two{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px 28px;align-items:start}
.three{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}
.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.panel{background:var(--surface);border:1px solid var(--ring);border-radius:14px;padding:4px 18px 10px}
.panel h3{margin-top:14px}
.panel p{font-size:14.5px;color:var(--ink-2)}
ul,ol{padding-left:20px;margin:0 0 12px}
li{margin:6px 0}
ul.ticks,ul.crosses{list-style:none;padding-left:0}
ul.ticks li,ul.crosses li{padding-left:26px;position:relative}
ul.ticks li::before{content:"";position:absolute;left:4px;top:.55em;width:10px;height:6px;border-left:2px solid var(--ok);
border-bottom:2px solid var(--ok);transform:rotate(-45deg)}
ul.crosses li::before,ul.crosses li::after{content:"";position:absolute;left:3px;top:.75em;width:13px;height:2px;
background:var(--no);transform:rotate(45deg)}
ul.crosses li::after{transform:rotate(-45deg)}
ol.loop li{margin:8px 0}
.splits{display:grid;grid-template-columns:minmax(0,440px) minmax(0,1fr);gap:24px 36px;align-items:start;margin-top:12px}
.splits-svg{width:100%;height:auto;display:block}
.sv-name{fill:var(--ink);font:600 14px system-ui,sans-serif}
.sv-legend{fill:var(--ink-2);font:13px system-ui,sans-serif}
.c-act{fill:var(--act)}.c-still{fill:var(--still)}.c-unknown{fill:var(--unknown)}.c-dead{fill:var(--dead)}
.c-claimed{fill:var(--claimed)}.c-idle{fill:var(--idle)}
.hatch{fill:url(#sv-hatch)}.hatch-line{fill:var(--hatch)}
ul.rules{padding-left:18px}
ul.rules li{margin:8px 0}
.tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:16px 0 8px}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:12px 14px;display:flex;
flex-direction:column}
.tile b{font-size:30px;line-height:1.1}
.tile span{font-size:14px}
.tile .muted{font-size:13px}
figure{margin:18px 0 6px}
figure img{display:block;max-width:100%;height:auto;border:1px solid var(--ring);border-radius:12px;background:#fff}
pre{font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;margin:10px 0 0;padding:10px 14px;
border-radius:10px;overflow-x:auto;white-space:pre}
pre.src{background:var(--code);border:1px solid var(--ring)}
pre.out{background:transparent;border:1px dashed var(--axis);color:var(--ink-2);margin-top:4px;font-size:12.5px}
pre + p,pre + h3{margin-top:14px}
code{font:13.5px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--code);padding:1px 5px;
border-radius:5px;overflow-wrap:anywhere}
pre code{background:none;padding:0}
.k,.kn,.kd,.kc,.kr{color:var(--tok-k)}.s,.s1,.s2,.sh,.sd,.dl{color:var(--tok-s)}.c,.c1,.ch,.cm{color:var(--tok-c);
font-style:italic}.nf,.nb,.nn,.nc,.nt,.nv{color:var(--tok-n)}.o,.p{color:var(--tok-o)}.mi,.mf{color:var(--tok-k)}
table{border-collapse:collapse;width:100%;font-size:14px}
th{text-align:left;font-weight:600;color:var(--muted);padding:6px 10px 6px 0;border-bottom:1px solid var(--line);
white-space:nowrap}
td{padding:6px 10px 6px 0;border-bottom:1px solid var(--line);vertical-align:top}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.scroll{overflow-x:auto}
.scroll td code{white-space:nowrap;overflow-wrap:normal}
table.verbs td:first-child{white-space:nowrap}
.chart{margin:12px 0 14px}
.strips{position:relative}
.strip{display:grid;grid-template-columns:110px minmax(0,1fr);gap:12px;align-items:center;margin:4px 0}
.strip-name{display:flex;flex-direction:column;font-size:13px;line-height:1.3}
.strip-name span{color:var(--muted);font-size:12px}
.strip-plot{height:60px}
.strip-plot svg{width:100%;height:100%;display:block;overflow:visible}
.xlab{position:relative;height:20px;margin-left:122px;font-size:12px;color:var(--muted)}
.xlab span{position:absolute;top:4px;transform:translateX(-50%)}
.xlab span:first-child{transform:none}.xlab span:last-child{transform:translateX(-100%)}
.grid{stroke:var(--line);stroke-width:1;vector-effect:non-scaling-stroke;stroke-dasharray:3 3}
.axis-line{stroke:var(--axis);stroke-width:1;vector-effect:non-scaling-stroke}
.line{fill:none;stroke-width:2;vector-effect:non-scaling-stroke;stroke-linejoin:round}
.area{stroke:none;opacity:.18}
.line.s1{stroke:var(--s1)}.line.s2{stroke:var(--s2)}.line.s3{stroke:var(--s3)}
.area.s1{fill:var(--s1)}.area.s2{fill:var(--s2)}.area.s3{fill:var(--s3)}
.cross{position:absolute;top:0;bottom:0;width:1px;background:var(--ink);opacity:.4;pointer-events:none}
.tip{position:absolute;top:0;background:var(--raised);border:1px solid var(--ring);border-radius:8px;padding:6px 10px;
font-size:12.5px;line-height:1.5;box-shadow:0 6px 24px rgba(0,0,0,.14);pointer-events:none;white-space:nowrap}
footer{border-top:1px solid var(--line);margin-top:40px;padding:22px 0 10px;color:var(--ink-2);font-size:14px}
footer p{margin:4px 0}
@media (max-width:860px){.glance,.grid3,.three{grid-template-columns:minmax(0,1fr)}
.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}.splits{grid-template-columns:minmax(0,1fr)}}
@media (max-width:640px){.strip{grid-template-columns:76px minmax(0,1fr)}
.xlab{margin-left:88px}h1{font-size:34px}.remit{font-size:20px}.lede{font-size:16.5px}h2{font-size:23px}
.two{grid-template-columns:minmax(0,1fr)}section{padding-top:32px}
table.verbs tr{display:block;padding:6px 0;border-bottom:1px solid var(--line)}
table.verbs td{display:block;border:0;padding:1px 0}table.verbs td:first-child{white-space:normal}pre{font-size:12px}pre.out{font-size:11.5px}}
"""

SCRIPT = """
(function(){
  document.querySelectorAll('[data-chart]').forEach(function(chart){
    var data=JSON.parse(chart.getAttribute('data-chart')),strips=chart.querySelector('.strips'),
        cross=strips.querySelector('.cross'),tip=strips.querySelector('.tip'),names=Object.keys(data);
    function clock(i){var m=i*5;return String(Math.floor(m/60)).padStart(2,'0')+':'+String(m%60).padStart(2,'0');}
    strips.addEventListener('pointermove',function(e){
      var plot=strips.querySelector('.strip-plot').getBoundingClientRect(),outer=strips.getBoundingClientRect();
      if(e.clientX<plot.left){cross.hidden=true;tip.hidden=true;return;}
      var f=Math.min(1,Math.max(0,(e.clientX-plot.left)/plot.width)),i=Math.round(f*(data[names[0]].length-1));
      var x=plot.left-outer.left+f*plot.width;
      cross.hidden=false;cross.style.left=x+'px';tip.hidden=false;
      tip.innerHTML='<b>'+clock(i)+'</b> (30-min mean)<br>'+names.map(function(n){
        return n+' '+Math.round(data[n][i]*100)+'% active';}).join('<br>');
      var left=x+12;if(left+tip.offsetWidth>outer.width){left=x-tip.offsetWidth-12;}
      tip.style.left=Math.max(0,left)+'px';
    });
    strips.addEventListener('pointerleave',function(){cross.hidden=true;tip.hidden=true;});
  });
})();
"""


def page(sections: list[str], hero_size: tuple[int, int], fleet_util: float) -> str:
    width, height = hero_size
    hero = (
        "<section class=\"hero\" id=\"top\"><p class=\"eyebrow\">robot_activity · v0.1.0 · MIT</p>"
        "<h1>Robot Activity Agent</h1>"
        f"<p class=\"remit\">{REMIT}</p>"
        "<p class=\"lede\">A robot that could be working and isn't is wasted capital, the way an idle press is in a "
        "plant. This is a drop-in member of any robotics team with exactly that remit: it measures activity per "
        "robot and per fleet, keeps a work queue full and moving, flags stalemates, and prompts the right people. "
        "It never decides <i>what</i> the work is.</p>"
        f"<div class=\"cta\"><a class=\"btn primary\" href=\"{REPO}\">View on GitHub</a>"
        "<a class=\"btn\" href=\"dashboard.html\">Open the demo dashboard</a>"
        "<a class=\"btn\" href=\"#dropin\">30-second quickstart</a></div>"
        "<div class=\"video\"><video autoplay muted loop playsinline controls preload=\"auto\" "
        f"poster=\"media/hero-poster.png\" width=\"{width}\" height=\"{height}\" "
        "aria-label=\"The dashboard replaying the demo fleet's day\">"
        "<source src=\"media/hero.mp4\" type=\"video/mp4\">"
        "<img src=\"media/hero.gif\" alt=\"The dashboard replaying the demo fleet's day\"></video></div>"
        "<p class=\"caption\">The real dashboard replaying the demo fleet's day: <code>dashboard(…, until=T)</code> "
        f"rendered every {FRAME_STEP_MIN} sim-minutes and screenshotted by headless Chrome, with the per-robot, "
        "stalemate and queue panels folded away so the agent's latest actions stay in frame. Simulated data. "
        "<a href=\"media/hero.gif\">GIF</a>.</p>"
        "<div class=\"glance\">"
        "<a class=\"card\" href=\"#metrics\"><b>Tool 1</b><strong>Metrics</strong><span>Utilisation per robot and fleet "
        f"({pct(fleet_util)} on the demo day), why each robot stood still, who held it, streaks, hand-offs, "
        "stalemates.</span></a>"
        "<a class=\"card\" href=\"#queue\"><b>Tool 2</b><strong>Queue + active queue</strong><span>Owners and "
        "executors, ready only once the code is shared, priorities, one screen for what runs, what's next and "
        "what's blocked.</span></a>"
        "<a class=\"card\" href=\"#wandb\"><b>Tool 3</b><strong>W&amp;B tracking</strong><span>One run per robot-day, "
        "one step per minute, the day's hours as the summary, a fleet run and a saved Report.</span></a>"
        "</div></section>")
    footer = (
        "<footer><p><strong>Hours moving is a proxy, not an objective.</strong> Pair it with a throughput KPI.</p>"
        f"<p>MIT licensed · <a href=\"{REPO}\">github.com/zacharyyamaoka/robot_activity</a> · "
        f"<a href=\"{REPO}/blob/main/docs/SPEC.md\">the contract</a> · "
        f"<a href=\"{REPO}/blob/main/CHANGELOG.md\">changelog</a></p>"
        "<p class=\"muted\">File formats: samples <code>robot-activity/1</code> · events ledger "
        "<code>robot-activity-event/1</code>. Every number, screen and frame on this page was produced by the package "
        "when the page was built.</p></footer>")
    description = f"Robot Activity Agent: {REMIT} Activity metrics, a work queue with owners, W&B tracking."
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<meta name=\"color-scheme\" content=\"light dark\">\n<title>Robot Activity Agent</title>\n"
            f"<link rel=\"icon\" href=\"{FAVICON}\">\n"
            f"<meta name=\"description\" content=\"{esc(description)}\">\n"
            "<meta property=\"og:title\" content=\"Robot Activity Agent\">\n"
            f"<meta property=\"og:description\" content=\"{esc(REMIT)}\">\n"
            f"<meta property=\"og:image\" content=\"{SITE}media/hero-poster.png\">\n"
            f"<style>{CSS}</style>\n</head>\n<body>\n<main>\n{hero}\n{''.join(sections)}\n{footer}\n</main>\n"
            f"<script>{SCRIPT}</script>\n</body>\n</html>\n")


# ---- checks --------------------------------------------------------------------------------------------------------

def check_page(browser: Browser) -> dict:
    """Render the built page wide and narrow, light and dark; fail on overflow; keep the screenshots."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    url = (OUT / "index.html").resolve().as_uri()
    overflow = "({doc: document.documentElement.scrollWidth, view: window.innerWidth})"
    report = {}
    for name, width, height, dark in (("site-1280", 1280, 900, False), ("site-390", 390, 844, False),
                                      ("site-1280-dark", 1280, 900, True), ("site-390-dark", 390, 844, True)):
        sizes = browser.measure(url, overflow, width=width, height=height, dark=dark)
        if sizes["doc"] > sizes["view"]:
            raise SystemExit(f"{name}: the page scrolls sideways ({sizes['doc']} px > {sizes['view']} px)")
        browser.shot(url, SHOTS / f"{name}.png", width=width, height=height, full_page=not dark, dark=dark)
        report[name] = str(SHOTS / f"{name}.png")
    errors = [entry for entry in browser.console_errors if "index.html" in entry[0] or "dashboard.html" in entry[0]]
    if errors:
        raise SystemExit(f"console errors: {errors}")
    return report


def privacy_scan() -> None:
    for path in OUT.rglob("*"):
        if path.suffix in (".html", ".json", ".txt", ".css", ".js") and PRIVATE.search(path.read_text("utf-8")):
            raise SystemExit(f"{path} holds a private path or secret: {PRIVATE.search(path.read_text()).group()}")


# ---- main ----------------------------------------------------------------------------------------------------------

def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="robot-activity-site-") as scratch:
        work = Path(scratch)
        store, queue = run_demo(work)
        check_replay_rebuild(queue, work)
        dashboard(store, queue, DAY, OUT / "dashboard.html")
        rows_by_robot = {robot: store.rows(robot, DAY) for robot in store.robots()}
        fleet = hours([row for rows in rows_by_robot.values() for row in rows])
        screen, at = queue_screen(queue, store, work)
        quickstart = run_quickstart(work)
        parity = real_parity()
        chart, runs = wandb_chart(store, queue)
        fleet_run = fleet_payload(store, DAY)
        with Browser() as browser:
            record_hero(browser, store, queue, work)
            capture_crops(browser)
            sections = [remit_section(), splits_section(fleet), metrics_section(store, rows_by_robot),
                        queue_section(screen, at), wandb_section(chart, runs, fleet_run), dropin_section(quickstart),
                        real_section(parity), shoulders_section()]
            (OUT / "index.html").write_text(page(sections, image_size(OUT / "media" / "hero-poster.png"),
                                                 fleet.utilization), encoding="utf-8")
            (OUT / ".nojekyll").write_text("")
            privacy_scan()
            shots = check_page(browser)
    sizes = {str(path.relative_to(OUT)): path.stat().st_size for path in sorted(OUT.rglob("*")) if path.is_file()}
    print(json.dumps({"out": str(OUT), "files": sizes, "total_bytes": sum(sizes.values()), "shots": shots,
                      "parity_worst_h": parity.get("worst")}, indent=1))


if __name__ == "__main__":
    main()
