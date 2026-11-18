"""agent.py: an ETA ask spends the owner's turn, so each not-ready item is asked about at most once per answer
(SPEC "agent.py", the ETA-asks paragraph, added 2026-11-18).

- An item whose owner gave an ETA that has not passed yet is not asked about, by either rule.
- Once that ETA is overdue, it is asked about again.
- One item is asked about by one rule per remind_s: `ask_eta` (dead streak) and `queue_low` never both ask about it.
"""

from __future__ import annotations

from helpers import NOON, series, write
from robot_activity.agent import ActivityAgent, AgentConfig
from robot_activity.queue import Queue
from robot_activity.store import Store


def dead(first_t: float, last_t: float):
    return series(first_t, last_t, robot="arm-1", claimed_by="sim-to-real", moving=False)


def make_agent(tmp_path, **config) -> ActivityAgent:
    return ActivityAgent(Store(tmp_path, tz="UTC"), Queue(tmp_path), AgentConfig(**config),
                         state_dir=tmp_path / "agent-state")


def asked_about(actions) -> list[tuple[str, tuple[str, ...]]]:
    return [(action.kind, action.asked) for action in actions if action.kind in ("ask_eta", "queue_low")]


def test_an_owner_with_a_future_eta_is_not_asked(tmp_path):
    write(Store(tmp_path, tz="UTC"), dead(NOON - 300, NOON))
    queue = Queue(tmp_path)
    queue.submit(title="sweep table", owner="sweep-agent", est_min=8.0, robot="arm-1", eta_t=NOON + 3600,
                 now=NOON - 900)
    assert asked_about(make_agent(tmp_path, min_runnable=1).tick(NOON)) == []


def test_an_overdue_eta_is_asked_about_again(tmp_path):
    write(Store(tmp_path, tz="UTC"), dead(NOON - 300, NOON))
    queue = Queue(tmp_path)
    sweep = queue.submit(title="sweep table", owner="sweep-agent", est_min=8.0, robot="arm-1", eta_t=NOON - 60,
                         now=NOON - 900)
    actions = make_agent(tmp_path, min_runnable=0).tick(NOON)
    assert asked_about(actions) == [("ask_eta", (sweep.id,))]
    assert actions[0].to == ("sweep-agent",)


def test_one_item_is_asked_about_by_one_rule_per_remind_window(tmp_path):
    store = Store(tmp_path, tz="UTC")
    write(store, dead(NOON - 300, NOON))
    queue = Queue(tmp_path)
    sweep = queue.submit(title="sweep table", owner="sweep-agent", est_min=8.0, robot="arm-1", now=NOON - 900)
    first = make_agent(tmp_path, min_runnable=1).tick(NOON)
    assert asked_about(first) == [("ask_eta", (sweep.id,))]  # dead streak asks first; queue_low does not repeat it
    write(store, dead(NOON + 10, NOON + 600))
    assert asked_about(make_agent(tmp_path, min_runnable=1).tick(NOON + 600)) == []  # within remind_s, either rule
    write(store, dead(NOON + 610, NOON + 1000))
    again = make_agent(tmp_path, min_runnable=1).tick(NOON + 1000)
    assert [asked for _, asked in asked_about(again)] == [(sweep.id,)]  # remind_s (900 s) later: asked once more


def test_a_new_item_is_still_asked_about_while_an_old_one_waits(tmp_path):
    store = Store(tmp_path, tz="UTC")
    write(store, dead(NOON - 300, NOON))
    queue = Queue(tmp_path)
    sweep = queue.submit(title="sweep table", owner="sweep-agent", est_min=8.0, robot="arm-1", now=NOON - 900)
    make_agent(tmp_path, min_runnable=0).tick(NOON)
    place = queue.submit(title="place bin", owner="ik-agent", est_min=6.0, robot="arm-1", now=NOON + 100)
    write(store, dead(NOON + 10, NOON + 200))
    actions = make_agent(tmp_path, min_runnable=0).tick(NOON + 200)
    assert asked_about(actions) == [("ask_eta", (place.id,))]
    assert sweep.id not in actions[0].message
