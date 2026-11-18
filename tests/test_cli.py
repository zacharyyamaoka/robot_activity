"""cli.py: the `robot-activity` entry point, driven as a subprocess (SPEC "cli.py")."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from procs import run_cli
from robot_activity.queue import Queue
from robot_activity.schema import Sample
from robot_activity.store import Store

DAY = "2026-11-18"


@pytest.fixture(scope="module")
def demo_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("cli_demo")
    result = run_cli("demo", "--root", str(root), "--day", DAY, "--seed", "3")
    assert result.returncode == 0, result.stderr
    return root


def test_demo_writes_a_day(demo_root):
    store = Store(demo_root, tz="UTC")
    assert len(store.robots()) >= 3
    assert all(store.rows(robot, DAY) for robot in store.robots())


def test_summary_json_is_one_document(demo_root):
    result = run_cli("summary", "--root", str(demo_root), "--day", DAY, "--json")
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    text = json.dumps(document)
    for robot in Store(demo_root, tz="UTC").robots():
        assert robot in text


def test_dashboard_out(demo_root, tmp_path):
    out = tmp_path / "activity.html"
    result = run_cli("dashboard", "--root", str(demo_root), "--day", DAY, "--out", str(out))
    assert result.returncode == 0, result.stderr
    assert "Why it stood still" in out.read_text()


def test_queue_submit_and_list(tmp_path):
    result = run_cli("queue", "submit", "--root", str(tmp_path), "--title", "cli pick vial", "--owner", "pnp-agent",
                     "--est-min", "4", "--robot", "arm-1", "--policy-repo", "https://github.com/example/pick-policies",
                     "--policy-commit", "0123456789abcdef", "--policy-path", "policies/pick.py",
                     "--policy-entrypoint", "run")
    assert result.returncode == 0, result.stderr
    (item,) = Queue(tmp_path).items()
    assert (item.title, item.owner, item.est_min, item.robot, item.source) == ("cli pick vial", "pnp-agent", 4.0, "arm-1",
                                                                              "agent")
    assert (item.policy.repo, item.policy.commit, item.policy.path, item.policy.entrypoint) == (
        "https://github.com/example/pick-policies", "0123456789abcdef", "policies/pick.py", "run")
    assert Queue(tmp_path).is_ready(item.id)

    listed = run_cli("queue", "list", "--root", str(tmp_path), "--json")
    assert listed.returncode == 0, listed.stderr
    document = json.loads(listed.stdout)
    assert "cli pick vial" in json.dumps(document)


def test_agent_tick_fills_from_the_config(tmp_path):
    now = time.time()
    store = Store(tmp_path)  # the CLI's store uses the system zone too
    for index in range(5):
        store.append(Sample(t=now - 40 + 10 * index, robot="arm-1", claimed_by="sim-to-real", moving=False))
    config = tmp_path / "agent.toml"
    config.write_text(
        '[[filler]]\ntitle = "cli self-test"\nowner = "rig-owner"\nest_min = 3.0\n'
        '[filler.policy]\nrepo = "https://github.com/example/rig"\ncommit = "abcdef0123456789"\npath = "selftest.py"\n')
    result = run_cli("agent", "tick", "--root", str(tmp_path), "--config", str(config))
    assert result.returncode == 0, result.stderr
    titles = [(item.title, item.source) for item in Queue(tmp_path).items()]
    assert titles == [("cli self-test", "filler")]


def test_usage_error_exits_2():
    result = run_cli("no-such-command")
    assert result.returncode == 2
    assert result.stderr.strip()


def test_queue_error_exits_2(tmp_path):
    result = run_cli("queue", "submit", "--root", str(tmp_path), "--title", "bad", "--owner", "o", "--est-min", "0")
    assert result.returncode == 2
    assert result.stderr.strip()
    assert Queue(tmp_path).items() == []
