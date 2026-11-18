"""agent.py: the agent's one loop, its de-dup state, config file and own KPI (SPEC "agent.py").

Every scenario builds a store and a queue in tmp_path, ticks the agent once (or a few times) and checks the actions,
the recipients, the side effects on the queue (only filler submits, never a start/finish/block) and the state files.
"""

from __future__ import annotations

import json
import re
import textwrap

import pytest

from helpers import NOON, policy, series, start_running, write
from robot_activity.agent import Action, ActivityAgent, AgentConfig, Filler, agent_kpis, load_config
from robot_activity.queue import Blocker, PolicyRef, Queue
from robot_activity.store import Store


class Recorder:
    """A notifier that keeps what it was handed."""

    def __init__(self):
        self.delivered: list[Action] = []

    def deliver(self, action: Action) -> bool:
        self.delivered.append(action)
        return True


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path, tz="UTC")


@pytest.fixture
def queue(tmp_path) -> Queue:
    return Queue(tmp_path)


def dead(first_t: float, last_t: float, *, robot: str = "arm-1", claimed_by: str | None = "sim-to-real"):
    """Claimed, nothing dispatched, still: dead rows."""
    return series(first_t, last_t, robot=robot, claimed_by=claimed_by, moving=False)


def make_agent(store, queue, tmp_path, recorder=None, **config) -> ActivityAgent:
    return ActivityAgent(store, queue, AgentConfig(**config), notifier=recorder, state_dir=tmp_path / "agent-state")


def kinds(actions) -> list[str]:
    return [action.kind for action in actions]


SELF_TEST = Filler(title="self-test", owner="rig-owner", est_min=3.0, policy=policy("selftest"))


# --- config and value types -----------------------------------------------------------------------------------------

def test_config_defaults():
    config = AgentConfig()
    assert (config.idle_after_s, config.min_runnable, config.remind_s, config.dispatcher, config.fillers,
            config.still_limit_s) == (180.0, 1, 900.0, "dispatcher", (), 300.0)
    assert SELF_TEST.robot == "any"


def test_action_defaults():
    action = Action(kind="queue_low", robot="arm-1", to=("a",), message="m")
    assert (action.item, action.policy, action.key) == (None, None, "")


def test_load_config_from_toml(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text(textwrap.dedent("""
        idle_after_s = 120.0
        min_runnable = 2
        remind_s = 600.0
        dispatcher = "w1-dispatcher"
        still_limit_s = 240.0

        [[filler]]
        title = "self-test"
        owner = "rig-owner"
        est_min = 3.0
        robot = "arm-1"
        [filler.policy]
        repo = "https://github.com/example/rig"
        commit = "abcdef0123456789"
        path = "selftest.py"
        entrypoint = "main"

        [[filler]]
        title = "recalibrate"
        owner = "calib-agent"
        est_min = 5
        [filler.policy]
        repo = "/srv/calib"
        commit = "fedcba9876543210"
        path = "calibrate.py"
    """))
    assert load_config(path) == AgentConfig(
        idle_after_s=120.0, min_runnable=2, remind_s=600.0, dispatcher="w1-dispatcher", still_limit_s=240.0,
        fillers=(
            Filler(title="self-test", owner="rig-owner", est_min=3.0, robot="arm-1",
                   policy=PolicyRef(repo="https://github.com/example/rig", commit="abcdef0123456789", path="selftest.py",
                                    entrypoint="main")),
            Filler(title="recalibrate", owner="calib-agent", est_min=5.0,
                   policy=PolicyRef(repo="/srv/calib", commit="fedcba9876543210", path="calibrate.py")),
        ))


def test_load_config_empty_file_is_the_defaults(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text("")
    assert load_config(str(path)) == AgentConfig()


# --- 1. dead streak -------------------------------------------------------------------------------------------------

def test_start_next_prompts_the_executor_with_the_policy(store, queue, tmp_path):
    write(store, dead(NOON - 300, NOON))
    item = queue.submit(title="pick vial", owner="pnp-agent", executor="rig-runner", est_min=37.0, robot="arm-1",
                        policy=policy(), now=NOON - 1000)
    recorder = Recorder()
    actions = make_agent(store, queue, tmp_path, recorder).tick(NOON)
    assert kinds(actions) == ["start_next"]
    action = actions[0]
    assert isinstance(action, Action)
    assert (action.robot, action.to, action.item) == ("arm-1", ("rig-runner",), item.id)
    assert action.policy == policy().describe()
    assert policy().describe() in action.message
    assert re.search(r"(?<![\w.])37(\.0+)?(?![\w.])", action.message), action.message  # its est_min
    assert item.id in action.message or "pick vial" in action.message
    assert action.key
    assert recorder.delivered == actions
    assert queue.get(item.id).status == "queued"  # the agent prompts; it never starts the item


def test_no_prompt_before_idle_after_s(store, queue, tmp_path):
    write(store, dead(NOON - 100, NOON))  # 110 s dead
    queue.submit(title="pick vial", owner="pnp-agent", est_min=4.0, robot="arm-1", policy=policy(), now=NOON - 1000)
    assert make_agent(store, queue, tmp_path).tick(NOON) == []
    assert kinds(make_agent(store, queue, tmp_path, idle_after_s=60.0).tick(NOON)) == ["start_next"]


def test_no_prompt_when_the_latest_row_is_stale(store, queue, tmp_path):
    write(store, dead(NOON - 900, NOON - 100))  # sampler stopped 100 s ago: untracked, never idle
    queue.submit(title="pick vial", owner="pnp-agent", est_min=4.0, robot="arm-1", policy=policy(), now=NOON - 1000)
    assert make_agent(store, queue, tmp_path).tick(NOON) == []


def test_no_prompt_while_the_robot_has_activity(store, queue, tmp_path):
    running = start_running(queue, started_t=NOON - 600)
    # still inside a job for 210 s: past idle_after_s, but activity (and under the 300 s stuck limit)
    write(store, series(NOON - 200, NOON, claimed_by="pnp-agent", item=running.id, moving=False))
    queue.submit(title="pick cap", owner="pnp-agent", est_min=4.0, robot="arm-1", policy=policy(), now=NOON - 1000)
    assert make_agent(store, queue, tmp_path).tick(NOON) == []


def test_ask_eta_to_up_to_three_not_ready_owners_with_reasons(store, queue, tmp_path):
    write(store, dead(NOON - 300, NOON))
    upstream = queue.submit(title="calibrate", owner="calib-agent", est_min=5.0, robot="arm-2", policy=policy("calib"),
                            now=NOON - 2000)
    queue.submit(title="sweep table", owner="sweep-agent", est_min=8.0, robot="arm-1", priority=1, now=NOON - 900)
    queue.submit(title="place bin", owner="ik-agent", est_min=6.0, robot="arm-1", priority=2, policy=policy("place"),
                 blockers=(Blocker(kind="code", why="IK refuses the bin pose"),), now=NOON - 900)
    queue.submit(title="pick vial", owner="pick-agent", est_min=4.0, robot="arm-1", priority=3, policy=policy(),
                 depends_on=(upstream.id,), now=NOON - 900)
    fourth = queue.submit(title="pick cap", owner="data-agent", est_min=4.0, robot="arm-1", priority=4, policy=policy(),
                          blockers=(Blocker(kind="data", why="no labels for the cap"),), now=NOON - 900)
    actions = make_agent(store, queue, tmp_path, min_runnable=0).tick(NOON)
    assert kinds(actions) == ["ask_eta"]
    action = actions[0]
    assert action.robot == "arm-1"
    assert action.to == ("sweep-agent", "ik-agent", "pick-agent")  # priority order, at most three items
    assert "no policy shared" in action.message
    assert "blocked on code: IK refuses the bin pose" in action.message
    assert f"waiting on dependency {upstream.id}" in action.message
    assert "no labels for the cap" not in action.message
    assert fourth.id not in action.message


def test_ask_eta_recipients_deduplicated_in_order(store, queue, tmp_path):
    write(store, dead(NOON - 300, NOON))
    for title, owner, priority in (("a", "sweep-agent", 1), ("b", "sweep-agent", 2), ("c", "ik-agent", 3)):
        queue.submit(title=title, owner=owner, est_min=1.0, robot="arm-1", priority=priority, now=NOON - 900)
    (action,) = make_agent(store, queue, tmp_path, min_runnable=0).tick(NOON)
    assert (action.kind, action.to) == ("ask_eta", ("sweep-agent", "ik-agent"))


# --- 2. queue low ---------------------------------------------------------------------------------------------------

def test_filler_submitted_ready_and_reported_to_dispatcher(store, queue, tmp_path):
    write(store, dead(NOON - 90, NOON))  # dead, but not long enough to prompt
    events_before = queue.events()
    actions = make_agent(store, queue, tmp_path, fillers=(SELF_TEST,)).tick(NOON)
    assert kinds(actions) == ["filled"]
    (filler,) = queue.items()
    assert (filler.title, filler.owner, filler.source, filler.priority, filler.robot, filler.est_min, filler.status) == (
        "self-test", "rig-owner", "filler", 9, "any", 3.0, "queued")
    assert filler.policy == policy("selftest")
    assert queue.is_ready(filler.id)
    action = actions[0]
    assert (action.robot, action.to, action.item, action.policy) == (
        "arm-1", ("dispatcher",), filler.id, policy("selftest").describe())
    new_events = queue.events()[len(events_before):]
    assert [(event["kind"], event["id"]) for event in new_events] == [("submit", filler.id)]


def test_filler_not_duplicated_while_queued_or_running(store, queue, tmp_path):
    write(store, dead(NOON - 90, NOON))
    make_agent(store, queue, tmp_path, fillers=(SELF_TEST,)).tick(NOON)
    write(store, dead(NOON + 10, NOON + 20))
    assert make_agent(store, queue, tmp_path, fillers=(SELF_TEST,)).tick(NOON + 20) == []  # still queued: not low
    (filler,) = queue.items()
    queue.start(filler.id, robot="arm-1", by="rig-owner", now=NOON + 30)  # the executor starts it, not the agent
    assert make_agent(store, queue, tmp_path, fillers=(SELF_TEST,)).tick(NOON + 40) == []  # running: same title, not re-filled
    assert [item.title for item in queue.items()] == ["self-test"]
    queue.finish(filler.id, now=NOON + 100)
    actions = make_agent(store, queue, tmp_path, fillers=(SELF_TEST,)).tick(NOON + 1000)
    assert kinds(actions) == ["filled"]
    assert sorted(item.status for item in queue.items()) == ["done", "queued"]


def test_filler_for_another_robot_is_not_submitted(store, queue, tmp_path):
    write(store, dead(NOON - 90, NOON))
    elsewhere = Filler(title="swing test", owner="rack-agent", est_min=2.0, policy=policy("swing"), robot="pendulum-1")
    assert make_agent(store, queue, tmp_path, fillers=(elsewhere,)).tick(NOON) == []
    assert queue.items() == []


def test_queue_low_asks_the_not_ready_owners(store, queue, tmp_path):
    write(store, dead(NOON - 90, NOON))
    queue.submit(title="pick vial", owner="pnp-agent", est_min=4.0, robot="arm-1", policy=policy(), now=NOON - 900)
    queue.submit(title="sweep table", owner="sweep-agent", est_min=8.0, robot="arm-1", priority=6, now=NOON - 900)
    queue.submit(title="place bin", owner="ik-agent", est_min=6.0, robot="arm-1", priority=3, now=NOON - 900)
    actions = make_agent(store, queue, tmp_path, min_runnable=2).tick(NOON)
    assert kinds(actions) == ["queue_low"]
    assert (actions[0].robot, actions[0].to) == ("arm-1", ("ik-agent", "sweep-agent"))


def test_no_queue_low_without_not_ready_items(store, queue, tmp_path):
    write(store, dead(NOON - 90, NOON))
    queue.submit(title="pick vial", owner="pnp-agent", est_min=4.0, robot="arm-1", policy=policy(), now=NOON - 900)
    assert make_agent(store, queue, tmp_path, min_runnable=2).tick(NOON) == []


def test_no_queue_low_once_fillers_fill_it(store, queue, tmp_path):
    write(store, dead(NOON - 90, NOON))
    queue.submit(title="sweep table", owner="sweep-agent", est_min=8.0, robot="arm-1", now=NOON - 900)
    assert kinds(make_agent(store, queue, tmp_path, fillers=(SELF_TEST,)).tick(NOON)) == ["filled"]


# --- 3. stalemate ---------------------------------------------------------------------------------------------------

def test_stalemate_goes_to_owner_and_dispatcher_with_the_policy(store, queue, tmp_path):
    running = start_running(queue, started_t=NOON - 1000, est_min=1.0, owner="pnp-agent")  # overrun: limit 210 s
    write(store, series(NOON - 120, NOON, claimed_by="pnp-agent", item=running.id, moving=True))
    actions = make_agent(store, queue, tmp_path, min_runnable=0, dispatcher="w1-dispatcher").tick(NOON)
    assert kinds(actions) == ["stalemate"]
    action = actions[0]
    assert (action.robot, action.to, action.item, action.policy) == (
        "arm-1", ("pnp-agent", "w1-dispatcher"), running.id, policy().describe())
    assert policy().describe() in action.message  # the reader can open the code and judge the behaviour


def test_stalemate_recipients_deduplicated(store, queue, tmp_path):
    running = start_running(queue, started_t=NOON - 1000, est_min=1.0, owner="dispatcher")
    write(store, series(NOON - 120, NOON, claimed_by="dispatcher", item=running.id, moving=True))
    (action,) = make_agent(store, queue, tmp_path, min_runnable=0).tick(NOON)
    assert (action.kind, action.to) == ("stalemate", ("dispatcher",))


def test_stuck_uses_the_configured_still_limit(store, queue, tmp_path):
    running = start_running(queue, started_t=NOON - 600, est_min=60.0)
    write(store, series(NOON - 200, NOON - 110, claimed_by="pnp-agent", item=running.id, moving=True))
    write(store, series(NOON - 100, NOON, claimed_by="pnp-agent", item=running.id, moving=False))  # 110 s still
    assert make_agent(store, queue, tmp_path, min_runnable=0).tick(NOON) == []
    assert kinds(make_agent(store, queue, tmp_path, min_runnable=0, still_limit_s=60.0).tick(NOON)) == ["stalemate"]


# --- de-dup, state and the ledger -----------------------------------------------------------------------------------

def test_dedup_survives_a_fresh_agent_and_reemits_after_remind_s(store, queue, tmp_path):
    item = queue.submit(title="pick vial", owner="pnp-agent", est_min=4.0, robot="arm-1", policy=policy(), now=NOON - 1000)
    write(store, dead(NOON - 300, NOON))
    first = make_agent(store, queue, tmp_path).tick(NOON)
    assert kinds(first) == ["start_next"]
    assert (tmp_path / "agent-state" / "state.json").exists()

    write(store, dead(NOON + 10, NOON + 60))
    assert make_agent(store, queue, tmp_path).tick(NOON + 60) == []  # a FRESH agent object reads state.json

    write(store, dead(NOON + 70, NOON + 960))
    again = make_agent(store, queue, tmp_path).tick(NOON + 960)
    assert kinds(again) == ["start_next"]
    assert again[0].item == item.id
    assert again[0].key == first[0].key

    lines = (tmp_path / "agent-state" / "actions.jsonl").read_text().splitlines()
    assert [json.loads(line)["t"] for line in lines] == [NOON, NOON + 960]


def test_actions_jsonl_in_the_default_state_dir(store, queue, tmp_path):
    write(store, dead(NOON - 300, NOON))
    queue.submit(title="pick vial", owner="pnp-agent", executor="rig-runner", est_min=4.0, robot="arm-1",
                 policy=policy(), now=NOON - 1000)
    actions = ActivityAgent(store, queue, AgentConfig()).tick(NOON)
    lines = (tmp_path / "agent" / "actions.jsonl").read_text().splitlines()
    assert len(lines) == len(actions) == 1
    logged = json.loads(lines[0])
    assert (logged["t"], logged["kind"], logged["robot"], logged["to"]) == (NOON, "start_next", "arm-1", ["rig-runner"])
    assert logged["item"] == actions[0].item
    assert logged["message"] == actions[0].message


def test_the_agent_never_starts_finishes_or_blocks(store, queue, tmp_path):
    write(store, dead(NOON - 400, NOON))
    runnable = queue.submit(title="pick vial", owner="pnp-agent", est_min=4.0, robot="arm-1", policy=policy(),
                            now=NOON - 1000)
    not_ready = queue.submit(title="sweep table", owner="sweep-agent", est_min=8.0, robot="arm-1", now=NOON - 1000)
    running = start_running(queue, started_t=NOON - 1000, est_min=1.0, robot="arm-2", owner="rack-agent")
    write(store, series(NOON - 120, NOON, robot="arm-2", claimed_by="rack-agent", item=running.id, moving=True))
    events_before = queue.events()
    actions = make_agent(store, queue, tmp_path, min_runnable=3, fillers=(SELF_TEST,)).tick(NOON)
    assert sorted(kinds(actions)) == ["filled", "queue_low", "stalemate", "start_next"]
    new_events = queue.events()[len(events_before):]
    assert new_events, "the filler submit is in the ledger"
    assert {event["kind"] for event in new_events} == {"submit"}
    assert {queue.get(event["id"]).source for event in new_events} == {"filler"}
    assert (queue.get(runnable.id).status, queue.get(not_ready.id).status, queue.get(running.id).status) == (
        "queued", "queued", "running")
    assert queue.get(not_ready.id).blockers == () and queue.get(not_ready.id).policy is None


# --- the agent's own KPI --------------------------------------------------------------------------------------------

def kpi_rows():
    return series(1010.0, 1600.0, claimed_by=None, moving=False)  # one dead streak, 1000 -> 1600 (600 s)


def test_kpis_without_any_prompt():
    result = agent_kpis(kpi_rows(), [])
    assert result["dead_h"] == pytest.approx(600 / 3600, abs=1e-9)
    assert result["idle_unprompted_s"] == pytest.approx(420.0, abs=1e-6)  # 600 s dead, 180 s allowed
    assert result["prompts"] == 0


def test_kpis_prompted_at_the_threshold():
    result = agent_kpis(kpi_rows(), [{"t": 1180.0, "robot": "arm-1", "kind": "start_next"}])
    assert result["idle_unprompted_s"] == pytest.approx(0.0, abs=1e-6)
    assert result["prompts"] == 1


def test_kpis_an_action_before_the_streak_still_covers_it():
    # literal reading: "no action for that robot in the preceding remind_s", wherever the action falls
    assert agent_kpis(kpi_rows(), [{"t": 900.0, "robot": "arm-1", "kind": "ask_eta"}])["idle_unprompted_s"] == (
        pytest.approx(0.0, abs=1e-6))


def test_kpis_ignore_other_robots_and_old_actions():
    actions = [{"t": 1180.0, "robot": "arm-2", "kind": "start_next"}, {"t": 100.0, "robot": "arm-1", "kind": "ask_eta"}]
    assert agent_kpis(kpi_rows(), actions)["idle_unprompted_s"] == pytest.approx(420.0, abs=1e-6)


def test_kpis_late_prompt_counts_the_wait():
    # 1180 -> 1300 unprompted; the boundary row may fall either side, so one sample (10 s) of slack
    result = agent_kpis(kpi_rows(), [{"t": 1300.0, "robot": "arm-1", "kind": "start_next"}])
    assert result["idle_unprompted_s"] == pytest.approx(120.0, abs=10.0)


def test_kpis_parameters():
    assert agent_kpis(kpi_rows(), [], idle_after_s=500.0)["idle_unprompted_s"] == pytest.approx(100.0, abs=1e-6)
    late = [{"t": 1180.0, "robot": "arm-1", "kind": "start_next"}]
    assert agent_kpis(kpi_rows(), late, remind_s=200.0)["idle_unprompted_s"] == pytest.approx(220.0, abs=10.0)
