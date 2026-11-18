"""views: the terminal queue screen and the one-file HTML dashboard (SPEC "views/")."""

from __future__ import annotations

import re
from pathlib import Path

from helpers import DAY, NOON, policy, series, write
from robot_activity.queue import Blocker, Queue
from robot_activity.store import Store
from robot_activity.views.html import dashboard
from robot_activity.views.terminal import render_queue

EXTERNAL = re.compile(
    r"""<script[^>]*\ssrc\s*=\s*["']?(https?:)?//"""
    r"""|<link[^>]*\shref\s*=\s*["']?(https?:)?//"""
    r"""|<img[^>]*\ssrc\s*=\s*["']?(https?:)?//"""
    r"""|url\(\s*["']?(https?:)?//"""
    r"""|@import\s+["']?(https?:)?//""",
    re.IGNORECASE)


def build_queue(root: Path) -> Queue:
    queue = Queue(root)
    running = queue.submit(title="calibrate wrist", owner="calib-agent", est_min=10.0, robot="arm-1",
                           policy=policy("calib"), now=NOON - 900)
    queue.start(running.id, robot="arm-1", by="calib-agent", now=NOON - 600)
    queue.submit(title="pick vial", owner="pnp-agent", est_min=4.0, source="system", policy=policy(), now=NOON - 500)
    queue.submit(title="pick cap", owner="pnp-agent", est_min=4.0, policy=policy(), now=NOON - 400)
    queue.submit(title="place bin", owner="ik-agent", est_min=6.0, robot="arm-1", policy=policy("place"),
                 blockers=(Blocker(kind="code", why="IK refuses the bin pose", eta_t=NOON + 1800),), now=NOON - 300)
    queue.submit(title="sweep table", owner="sweep-agent", est_min=8.0, now=NOON - 200)
    queue.submit(title="swing pendulum", owner="rack-agent", est_min=8.0, robot="pendulum-1", policy=policy("swing"),
                 now=NOON - 100)
    return queue


def test_render_queue_one_screen(tmp_path):
    text = render_queue(build_queue(tmp_path), robot="arm-1", now=NOON)
    assert isinstance(text, str)
    assert "\x1b[" not in text  # plain text by default
    for needle in ("calibrate wrist", "calib-agent", "pick vial", "pick cap", "place bin", "ik-agent",
                   "blocked on code: IK refuses the bin pose", "sweep table", "sweep-agent", "no policy shared"):
        assert needle in text, needle
    # what runs now, then the runnable order, then the not-ready items
    assert text.index("calibrate wrist") < text.index("pick vial") < text.index("pick cap")
    assert text.index("pick cap") < text.index("place bin")
    assert text.index("pick cap") < text.index("sweep table")
    assert "swing pendulum" not in text  # another robot's item


def test_render_queue_all_robots(tmp_path):
    text = render_queue(build_queue(tmp_path))
    assert "swing pendulum" in text and "calibrate wrist" in text


def build_day(root: Path) -> tuple[Store, Queue]:
    store = Store(root, tz="UTC")
    queue = build_queue(root)
    write(store, series(NOON - 3600, NOON - 1800, claimed_by="pnp-agent", moving=False, reason="EARLY REASON blocked on code"))
    write(store, series(NOON - 1790, NOON - 600, claimed_by="pnp-agent", item="job-x", moving=True))
    write(store, series(NOON - 590, NOON, claimed_by="pnp-agent", moving=False, reason="LATE REASON bus down"))
    write(store, series(NOON - 3600, NOON, robot="pendulum-1", moving=None))
    return store, queue


def tree(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_dashboard_is_one_self_contained_file(tmp_path):
    root = tmp_path / "root"
    store, queue = build_day(root)
    out = tmp_path / "out" / "dashboard.html"
    out.parent.mkdir()
    before = tree(root)
    written = dashboard(store, queue, DAY, out, now=NOON)
    assert Path(written) == out
    assert [path.name for path in out.parent.iterdir()] == ["dashboard.html"]
    assert tree(root) == before  # reads the state, writes nothing beside the one file
    html = out.read_text()
    assert not EXTERNAL.search(html), EXTERNAL.search(html)
    for title in ("Why it stood still", "Who held the robot"):
        assert title in html, title
    lowered = html.lower()
    for word in ("fleet", "stalemate", "queue"):
        assert word in lowered, word
    assert re.search(r"hand-?off", lowered)
    for needle in ("arm-1", "pendulum-1", "EARLY REASON blocked on code", "LATE REASON bus down", "pnp-agent"):
        assert needle in html, needle


def test_dashboard_until_cuts_later_rows(tmp_path):
    root = tmp_path / "root"
    store, queue = build_day(root)
    out = tmp_path / "frame.html"
    dashboard(store, queue, DAY, out, now=NOON, until=NOON - 1200)
    html = out.read_text()
    assert "EARLY REASON blocked on code" in html
    assert "LATE REASON bus down" not in html
