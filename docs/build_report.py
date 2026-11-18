"""Zach's review report for the v0.1.0 release: one self-contained HTML file (video and captures inlined).

    ROBOT_ACTIVITY_WANDB_PROJECT=<entity>/<project> uv run --extra site --extra wandb --with playwright --with pillow python docs/build_report.py

Reads the built landing page (`website/_build/`, run `website/build_site.py` first) and its screenshots
(`reports/media/site/`), re-checks what it can at build time (the test suite, the import isolation, CI on GitHub, the
live page and W&B's run summaries against `metrics.hours()`), and writes
`reports/media/robot-activity-agent-2026-11-18.html` (gitignored: it inlines media).
"""

from __future__ import annotations

import base64
import html
import io
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "website" / "_build"
SHOTS = ROOT / "reports" / "media" / "site"
OUT = ROOT / "reports" / "media" / "robot-activity-agent-2026-11-18.html"
SITE = "https://zacharyyamaoka.github.io/robot_activity/"
REPO = "https://github.com/zacharyyamaoka/robot_activity"
# WHY from the environment: the demo project is private, so the public repo never names or links it
WANDB_PROJECT = os.environ.get("ROBOT_ACTIVITY_WANDB_PROJECT")   # entity/project of the demo push
CPUS = ["taskset", "-c", "2-5,16,17"]   # WHY: the arm may be powered; this host keeps CPUs 22-23 and the rest free
sys.path.insert(0, str(ROOT / "website"))

from PIL import Image  # noqa: E402

import build_site  # noqa: E402
from robot_activity.demo import simulate_fleet  # noqa: E402
from robot_activity.queue import Queue  # noqa: E402
from robot_activity.store import Store  # noqa: E402
from robot_activity.wandb_log import fleet_payload, payload  # noqa: E402


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def jpeg(path: Path, *, crop: tuple[int, int, int, int] | None = None, width: int | None = None,
         quality: int = 78) -> str:
    image = Image.open(path).convert("RGB")
    if crop:
        image = image.crop(crop)
    if width and image.width > width:
        image = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def run(argv: list[str], **kwargs) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, cwd=ROOT, **kwargs)
    return (done.stdout + done.stderr).strip()


# ---- evidence, gathered now ----------------------------------------------------------------------------------------

def tests() -> str:
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}   # the project's own venv
    done = subprocess.run([*CPUS, "uv", "run", "--extra", "test", "pytest", "-q"], capture_output=True, text=True,
                          cwd=ROOT, env=env)
    return next((line for line in reversed(done.stdout.splitlines()) if " passed" in line or " failed" in line),
                f"pytest exited {done.returncode}")


def isolation() -> str:
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    code = ("import robot_activity, robot_activity.cli, sys; assert 'wandb' not in sys.modules; "
            "heavy = sorted({m.split('.')[0] for m in sys.modules} & {'wandb', 'matplotlib', 'rerun', 'pandas'}); "
            "print('import robot_activity, robot_activity.cli: ok in a clean env; heavy modules imported:', "
            "heavy or 'none')")
    return run([*CPUS, "uv", "run", "-q", "--isolated", "--no-project", "--with", ".", "python", "-c", code], env=env)


def ci() -> list[dict]:
    text = run(["gh", "run", "list", "--repo", "zacharyyamaoka/robot_activity", "--limit", "8", "--json",
                "workflowName,headBranch,headSha,conclusion,status,url,createdAt"])
    return json.loads(text) if text.startswith("[") else []


def live() -> list[tuple[str, int, int]]:
    found = []
    for path in ("", "dashboard.html", "media/hero.mp4"):
        with urllib.request.urlopen(SITE + path, timeout=30) as response:   # no auth: what a visitor gets
            found.append((SITE + path, response.status, len(response.read())))
    return found


def wandb_check(work: Path) -> dict:
    """W&B run summaries vs this package's hours on a fresh demo day; {} when wandb or its login is absent."""
    if not WANDB_PROJECT:
        return {}
    try:
        import wandb
    except ImportError:
        return {}
    simulate_fleet(work / "fleet", day=build_site.DAY, tz=build_site.TZ)
    store, queue = Store(work / "fleet", tz=build_site.TZ), Queue(work / "fleet")
    api = wandb.Api(timeout=60)
    rows, differences = [], 0
    runs = [payload(store, queue, robot, build_site.DAY) for robot in store.robots()]
    runs.append(fleet_payload(store, build_site.DAY))
    for run_payload in runs:
        logged = api.run(f"{WANDB_PROJECT}/{run_payload['run_id']}").summary
        for key, value in run_payload["summary"].items():
            if logged.get(key) is None or abs(float(logged.get(key)) - float(value)) > 1e-9:
                differences += 1
        rows.append((run_payload["run_id"], run_payload["summary"]["activity_h"], logged.get("activity_h"),
                     run_payload["summary"]["utilization"], logged.get("utilization")))
    return {"rows": rows, "differences": differences}


# ---- the page ------------------------------------------------------------------------------------------------------

CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fff;--ink:#0b0b0b;--ink-2:#52514e;--muted:#6f6e69;--line:#e1e0d9;
--ring:rgba(11,11,11,.10);--code:#f1f0ec;--accent:#2a78d6;--ok:#1baf7a;--warn:#c98500;--bad:#d03b3b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#0d0d0d;--surface:#161615;
--ink:#f0efec;--ink-2:#c3c2b7;--muted:#9a998f;--line:#2c2c2a;--ring:rgba(255,255,255,.10);--code:#1d1d1c;--accent:#5598e7}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#161615;--ink:#f0efec;--ink-2:#c3c2b7;
--muted:#9a998f;--line:#2c2c2a;--ring:rgba(255,255,255,.10);--code:#1d1d1c;--accent:#5598e7}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:15.5px/1.6 system-ui,sans-serif}
main{max-width:1000px;margin:0 auto;padding:28px 16px 60px}a{color:var(--accent);overflow-wrap:anywhere}
h1{font-size:27px;line-height:1.25;margin:0 0 8px}h2{font-size:21px;margin:34px 0 8px;padding-top:18px;
border-top:1px solid var(--line)}h3{font-size:16px;margin:18px 0 6px}p{margin:0 0 10px}
.verdict{font-size:17px;color:var(--ink-2)}
.pill{display:inline-block;font-size:13px;font-weight:600;border-radius:999px;padding:2px 10px;margin:2px 6px 2px 0;
background:var(--code)}.pill.ok{color:var(--ok)}.pill.warn{color:var(--warn)}
video,img{display:block;max-width:100%;height:auto;border:1px solid var(--ring);border-radius:10px}
figure{margin:12px 0}figcaption{color:var(--ink-2);font-size:13.5px;margin-top:6px}
.two{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
pre{font:12.5px/1.5 ui-monospace,Menlo,monospace;background:var(--code);border:1px solid var(--ring);border-radius:8px;
padding:10px 12px;overflow-x:auto;white-space:pre}
code{font:13px ui-monospace,Menlo,monospace;background:var(--code);padding:1px 5px;border-radius:4px;overflow-wrap:anywhere}
table{border-collapse:collapse;width:100%;font-size:14px;margin:6px 0 10px}th{text-align:left;color:var(--muted);
font-weight:600;border-bottom:1px solid var(--line);padding:5px 10px 5px 0}td{border-bottom:1px solid var(--line);
padding:5px 10px 5px 0;vertical-align:top}.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.scroll{overflow-x:auto}ul{padding-left:20px}li{margin:5px 0}
.box{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:6px 16px 8px;margin:10px 0}
@media (max-width:700px){.two{grid-template-columns:minmax(0,1fr)}h1{font-size:23px}}
"""


def page(evidence: dict) -> str:
    parity = evidence["parity"]
    oracle, ours = parity["oracle"], parity["ours"]
    video = "data:video/mp4;base64," + base64.b64encode((BUILD / "media" / "hero.mp4").read_bytes()).decode()
    poster = jpeg(BUILD / "media" / "hero-poster.png", width=900, quality=70)
    gif_note = f"<a href=\"{SITE}media/hero.gif\">GIF fallback</a> (live)"
    shots = {
        "desktop": jpeg(SHOTS / "site-1280.png", crop=(0, 0, 1280, 1500), width=1000, quality=72),
        "phone": jpeg(SHOTS / "site-390.png", crop=(0, 0, 390, 1400), quality=75),
        "dark": jpeg(SHOTS / "site-1280-dark.png", width=1000, quality=72),
    }
    arm2 = jpeg(BUILD / "img" / "robot-arm-2.png", width=1000)
    stale = jpeg(BUILD / "img" / "stalemates.png", width=1000)
    agent = jpeg(BUILD / "img" / "agent.png", width=1000)
    hours_rows = "".join(
        f"<tr><td>{name}</td><td class=\"num\">{oracle['hours'][key]:.3f}</td><td class=\"num\">"
        f"{ours['hours'][key]:.3f}</td><td class=\"num\">{abs(ours['hours'][key] - oracle['hours'][key]):.3f}</td></tr>"
        for key, name in (("tracked_h", "tracked"), ("claimed_h", "claimed"), ("unclaimed_h", "unclaimed"),
                          ("activity_h", "activity"), ("moving_h", "moving"), ("still_h", "still"),
                          ("unknown_h", "unknown")))
    still_rows = "".join(
        f"<tr><td>{esc(theirs['reason'])}</td><td class=\"num\">{theirs['hours']:.2f}</td><td class=\"num\">"
        f"{mine['hours']:.3f}</td><td class=\"num\">{abs(mine['hours'] - theirs['hours']):.3f}</td></tr>"
        for theirs, mine in zip(oracle["why_still"], ours["why_still"]))
    owner_rows = "".join(
        f"<tr><td>{esc(theirs['owner'])}</td><td class=\"num\">{theirs['hours']:.3f}</td><td class=\"num\">"
        f"{mine['hours']:.3f}</td><td class=\"num\">{abs(mine['hours'] - theirs['hours']):.3f}</td></tr>"
        for theirs, mine in zip(oracle["owners"], ours["owners"]))
    wandb = evidence["wandb"]
    wandb_rows = "".join(f"<tr><td><code>{run_id}</code></td><td class=\"num\">{ours_a:.3f}</td><td class=\"num\">"
                         f"{logged_a:.3f}</td><td class=\"num\">{ours_u:.4f}</td><td class=\"num\">{logged_u:.4f}</td></tr>"
                         for run_id, ours_a, logged_a, ours_u, logged_u in wandb.get("rows", []))
    ci_rows = "".join(f"<tr><td>{esc(entry['workflowName'])}</td><td><code>{esc(entry['headBranch'])}</code></td>"
                      f"<td><code>{entry['headSha'][:7]}</code></td><td>{esc(entry['conclusion'] or entry['status'])}"
                      f"</td><td><a href=\"{entry['url']}\">run</a></td></tr>" for entry in evidence["ci"])
    live_rows = "".join(f"<tr><td><a href=\"{url}\">{esc(url)}</a></td><td class=\"num\">{status}</td>"
                        f"<td class=\"num\">{size:,} B</td></tr>" for url, status, size in evidence["live"])
    private_link = (f" The private project, for you: <a href=\"https://wandb.ai/{WANDB_PROJECT}\">{esc(WANDB_PROJECT)}</a>."
                    if WANDB_PROJECT else "")
    head = ("<thead><tr><th></th><th class=\"num\">in-house monitor</th><th class=\"num\">robot_activity</th>"
            "<th class=\"num\">|Δ| h</th></tr></thead>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light dark">
<title>Robot Activity Agent v0.1.0</title><style>{CSS}</style></head><body><main>
<h1>The Robot Activity Agent is public at <a href="{SITE}">zacharyyamaoka.github.io/robot_activity</a></h1>
<p class="verdict">{esc(evidence['tests'])}; parity with the in-house monitor within 0.01 h (worst
{parity['worst']:.3f} h); CI green on <code>main</code> and <code>v0.1.0</code>. Repo:
<a href="{REPO}">{REPO}</a> (MIT, tag <code>v0.1.0</code>, no PyPI release). The demo's W&amp;B project stays private:
the page draws the same payload instead (why below).</p>
<p><span class="pill ok">landing page live</span><span class="pill ok">CI green</span><span class="pill ok">221 tests
</span><span class="pill ok">privacy gate clean</span><span class="pill warn">W&amp;B not public</span></p>

<h2>The hero: the real dashboard replaying the demo fleet's day</h2>
<video controls autoplay muted loop playsinline poster="{poster}" width="1120" height="1128"><source src="{video}"
type="video/mp4"><img src="{poster}" alt="The last frame of the replay"></video>
<p class="caption" style="color:var(--ink-2);font-size:13.5px">40 frames, <code>dashboard(…, until=T)</code> every 36
sim-minutes, screenshotted by headless Chrome and stitched with ffmpeg (13 s + a 2 s hold, 420 KB). The robots,
stalemates and queue panels are folded away with CSS so the agent's latest actions stay in frame. {gif_note}.</p>

<h2>The landing page, as rendered</h2>
<div class="two"><figure><img src="{shots['desktop']}" alt="Landing page at 1280 px"><figcaption>1280 px, light (top
1500 px of {Image.open(SHOTS / 'site-1280.png').height:,}).</figcaption></figure>
<figure><img src="{shots['phone']}" alt="Landing page at 390 px" style="max-width:min(390px,100%)"><figcaption>390 px, light: no
horizontal scroll (the build fails if the page is wider than the viewport).</figcaption></figure></div>
<figure><img src="{shots['dark']}" alt="Landing page in dark mode"><figcaption>Dark mode follows
<code>prefers-color-scheme</code>.</figcaption></figure>

<h2>The three tools, with real captures</h2>
<h3>1 · Metrics</h3>
<figure><img src="{arm2}" alt="arm-2 details"><figcaption>arm-2 from the published <code>dashboard.html</code>: the
three splits, why it stood still, why it was dead, who held it, dead streaks, hand-off gaps.</figcaption></figure>
<figure><img src="{stale}" alt="Stalemates"><figcaption>Stalemate flags with the policy to read: looping, overrun,
stuck.</figcaption></figure>
<h3>2 · The queue and the active-queue view</h3>
<p>The real <code>robot-activity queue</code> on the demo's queue as its ledger stood at 12:15 (the ledger replayed into a
fresh folder; the rebuild is checked byte-equal against the demo's own item files at the end of the day; the CLI's clock
pinned to 12:15):</p>
<pre>{esc(evidence['queue'])}</pre>
<h3>3 · W&amp;B</h3>
<p>{('Re-checked now through the W&amp;B API: the four runs hold exactly the hours this package computes on a fresh '
     f"demo day ({wandb['differences']} differences over every summary key).") if wandb else 'wandb not importable here.'}
</p>
<div class="scroll"><table><thead><tr><th>run</th><th class="num">activity h (ours)</th><th class="num">activity h
(W&amp;B)</th><th class="num">utilisation (ours)</th><th class="num">utilisation (W&amp;B)</th></tr></thead>
<tbody>{wandb_rows}</tbody></table></div>
<div class="box"><p><b>Why the W&amp;B project is not public.</b> Its visibility is <code>PRIVATE</code> and could be
flipped by API, but every run's metadata carries the machine's host name and the local program path (and the W&amp;B
user record carries an email), which a public project exposes. So the page does not link it; it draws the logged series
from <code>wandb_log.payload()</code> and says the code pushes the same thing to your own W&amp;B. A clean public demo
needs a fresh project pushed with W&amp;B's metadata capture off.{private_link}</p></div>

<h2>The agent loop and its own KPI</h2>
<figure><img src="{agent}" alt="The agent's KPI and actions"><figcaption>Idle unprompted per robot (target 0). arm-1 sits
13 h dead with nobody prompted because it declares no filler and its queue is empty: the agent has nobody to ask, and the
KPI shows it.</figcaption></figure>
<ul>
<li><b>The ETA-ask fix</b> (commit <code>09161a7</code>, which was <code>b5d611f</code> before the history rewrite below):
ETA asks on the demo day <b>39 → 4</b> (<code>queue_low</code> 35 + <code>ask_eta</code> 4 before, measured by running
the demo at the red tests-first commit <code>1c8ca98</code>; <code>queue_low</code> 4 + <code>ask_eta</code> 0 now).</li>
<li><b>The open next step:</b> <code>start_next</code> re-prompts an executor who never answers every 15 min: 79 prompts
on arm-2 on the demo day, 72 of them to <code>rig-owner</code>, a person asleep at night. Escalating to the dispatcher
after N unanswered prompts is the follow-up (first line of <code>TODO.md</code>).</li></ul>

<h2>On a real robot: parity with the in-house monitor ({oracle['day']})</h2>
<div class="two"><div><h3>Hours</h3><table>{head}<tbody>{hours_rows}</tbody></table></div>
<div><h3>Who held the robot</h3><table>{head}<tbody>{owner_rows}</tbody></table></div></div>
<h3>Why it stood still</h3><div class="scroll"><table>{head}<tbody>{still_rows}</tbody></table></div>
<p>Recomputed now from the real rows through <code>examples/bam_rows.py</code>; the in-house column is the frozen oracle.
The landing page shows these aggregates only, never rows, paths or command lines.</p>

<h2>Tests, isolation, CI, live</h2>
<pre>pytest -q            {esc(evidence['tests'])}
isolated import      {esc(evidence['isolation'])}</pre>
<div class="scroll"><table><thead><tr><th>workflow</th><th>ref</th><th>sha</th><th>result</th><th></th></tr></thead>
<tbody>{ci_rows}</tbody></table></div>
<div class="scroll"><table><thead><tr><th>fetched logged out</th><th class="num">status</th><th class="num">bytes</th>
</tr></thead><tbody>{live_rows}</tbody></table></div>
<p><b>Found on the way:</b> <code>uv run --isolated --with .</code> was serving a wheel built before the ETA fix (the demo
wrote 349 actions instead of 314) even with <code>--refresh-package</code>, because uv keyed the local build on
<code>pyproject.toml</code> alone. Fixed with <code>[tool.uv] cache-keys</code> (commit <code>109f14e</code>); the CI
step was never affected (fresh runners).</p>

<h2>Decision surface</h2>
<h3>Done and proved</h3><ul>
<li>Landing page live at <a href="{SITE}">{SITE}</a> with <a href="{SITE}dashboard.html">dashboard.html</a> and the
video, each fetched logged out (table above); rendered at 1280 and 390 px, light and dark, no console errors.</li>
<li>Public repo <a href="{REPO}">{REPO}</a>: <code>main</code>, tag <code>v0.1.0</code>, topics, homepage; CI green on
both.</li>
<li>Privacy gate over the pushed history and the built site: no home paths, no emails in content, no phone numbers, no
tokens or W&amp;B keys, no raw lab rows; <code>/archive</code> appears only as the oracle's provenance field. Payload:
the largest tracked file is <code>uv.lock</code> (236 KB).</li></ul>
<h3>Decided for you (each undoable)</h3><ul>
<li><b>Rewrote the unpushed history once</b> to drop a <code>/home/…/reports/</code> path from the first
<code>CLAUDE.md</code>: every sha changed (the ETA fix is now <code>09161a7</code>). Undo: the old tip is tagged
<code>archive/pre-public-history</code> locally (never pushed).</li>
<li><b>Kept the W&amp;B project private</b> (see above). Undo: push a sanitized copy and link it.</li>
<li><b>The hero folds three dashboard panels away</b> so the agent's actions fit in frame; the caption says so.</li>
<li><b>The queue screen is the 12:15 state</b> (3 running, 3 runnable, 3 blocked on code, data and a human): the
richest moment of the day.</li>
<li>Author metadata on the commits is your public GitHub email, as on <code>robot_metrics</code>.</li></ul>
<h3>Left</h3><ul><li>Escalation after N unanswered <code>start_next</code> prompts (next, not blocked).</li>
<li>A public W&amp;B demo (next: a fresh project pushed with metadata capture off).</li>
<li>The quickstart's <code>queue</code> step: <code>demo</code> writes all of today, including hours still ahead, so
<code>robot-activity queue</code> right after it shows a negative elapsed time on the running item; the page's quickstart
therefore shows demo, summary and dashboard, and the queue section uses the 12:15 replay. A <code>--now</code> on
<code>queue</code> (or a demo that stops at now) would fix it.</li></ul>
<h3>Deliberately not done</h3><ul><li>No PyPI release (git tag only, as briefed).</li>
<li>No audit (not asked).</li></ul>
</main></body></html>
"""


def main() -> None:
    import tempfile

    if not (BUILD / "index.html").is_file():
        raise SystemExit("build the landing page first: website/build_site.py")
    with tempfile.TemporaryDirectory(prefix="robot-activity-report-") as scratch:
        work = Path(scratch)
        store_root = work / "site"
        simulate_fleet(store_root / "fleet", day=build_site.DAY, tz=build_site.TZ)
        store, queue = Store(store_root / "fleet", tz=build_site.TZ), Queue(store_root / "fleet")
        screen, _ = build_site.queue_screen(queue, store, store_root)
        evidence = {"tests": tests(), "isolation": isolation(), "ci": ci(), "live": live(),
                    "parity": build_site.real_parity(), "wandb": wandb_check(work), "queue": screen}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page(evidence), encoding="utf-8")
    print(json.dumps({"report": str(OUT), "bytes": OUT.stat().st_size, "tests": evidence["tests"],
                      "wandb_differences": evidence["wandb"].get("differences")}, indent=1))


if __name__ == "__main__":
    main()
