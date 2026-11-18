"""queue.py: the work queue, its files, readiness rules and the frozen events ledger (SPEC "queue.py")."""

from __future__ import annotations

import json
import multiprocessing

import pytest

from helpers import NOON, policy, submit_many
from robot_activity.queue import BLOCKER_KINDS, DEFAULT_PRIORITY, Blocker, PolicyRef, Queue, QueueError
from robot_activity.schema import QueueSnapshot

EVENT_SCHEMA = "robot-activity-event/1"
BASE_KEYS = {"schema", "t", "kind", "id", "robot", "by"}


@pytest.fixture
def queue(tmp_path) -> Queue:
    return Queue(tmp_path)


def ready(queue: Queue, title: str, **fields):
    fields.setdefault("owner", "pnp-agent")
    fields.setdefault("est_min", 4.0)
    fields.setdefault("policy", policy())
    return queue.submit(title=title, **fields)


def ids(items) -> set[str]:
    return {item.id for item in items}


# --- constants and value types --------------------------------------------------------------------------------------

def test_constants():
    assert BLOCKER_KINDS == ("code", "dependency", "human", "hardware", "data", "other")
    assert DEFAULT_PRIORITY == {"system": 0, "agent": 5, "filler": 9}


def test_policy_describe_with_entrypoint():
    ref = PolicyRef(repo="https://github.com/example/picks", commit="0123456789abcdef0123", path="policies/pick.py",
                    entrypoint="run")
    assert ref.describe() == "https://github.com/example/picks@0123456789ab:policies/pick.py::run"


def test_policy_describe_without_entrypoint():
    ref = PolicyRef(repo="/srv/rig", commit="0123456789abcdef0123", path="tools/selftest.py")
    assert ref.describe() == "/srv/rig@0123456789ab:tools/selftest.py"


def test_policy_describe_short_commit_kept_whole():
    assert PolicyRef(repo="r", commit="abc123", path="p.py").describe() == "r@abc123:p.py"


@pytest.mark.parametrize("kind", BLOCKER_KINDS)
def test_blocker_known_kinds(kind):
    blocker = Blocker(kind=kind, why="because")
    assert blocker.kind == kind
    assert blocker.eta_t is None


def test_blocker_unknown_kind_is_value_error():
    with pytest.raises(ValueError):
        Blocker(kind="weather", why="rain")


# --- submit ---------------------------------------------------------------------------------------------------------

def test_submit_defaults(queue):
    item = queue.submit(title="pick vial", owner="pnp-agent", est_min=4.0, now=NOON)
    assert item.title == "pick vial"
    assert item.owner == "pnp-agent"
    assert item.executor == "pnp-agent"  # executor defaults to the owner
    assert item.robot == "any"
    assert item.source == "agent"
    assert item.priority == 5
    assert item.est_min == 4.0
    assert item.status == "queued"
    assert item.submitted_t == NOON
    assert item.depends_on == ()
    assert item.policy is None
    assert item.blockers == ()
    assert (item.started_t, item.finished_t, item.started_by, item.ran_on, item.result) == (None, None, None, None, None)
    assert item.eta_t is None and item.note is None


@pytest.mark.parametrize("source, priority", [("system", 0), ("agent", 5), ("filler", 9)])
def test_priority_defaults_by_source(queue, source, priority):
    assert queue.submit(title="t", owner="o", est_min=1.0, source=source).priority == priority


def test_explicit_priority_and_executor(queue):
    item = queue.submit(title="t", owner="planner", executor="rig-runner", est_min=1.0, source="filler", priority=2,
                        robot="arm-1", eta_t=NOON + 60, note="try the slow profile")
    assert (item.executor, item.priority, item.robot, item.eta_t, item.note) == (
        "rig-runner", 2, "arm-1", NOON + 60, "try the slow profile")


@pytest.mark.parametrize("est_min", [0, 0.0, -1.0])
def test_est_min_must_be_positive(queue, est_min):
    with pytest.raises(QueueError):
        queue.submit(title="t", owner="o", est_min=est_min)
    assert queue.items() == []
    assert queue.events() == []


def test_one_json_file_per_item(queue, tmp_path):
    item = ready(queue, "pick vial", now=NOON)
    path = tmp_path / "queue" / "items" / f"{item.id}.json"
    assert path.exists()
    stored = json.loads(path.read_text())
    assert stored["id"] == item.id
    assert stored["title"] == "pick vial"
    assert stored["status"] == "queued"
    queue.start(item.id, robot="arm-1", by="pnp-agent", now=NOON + 1)
    assert json.loads(path.read_text())["status"] == "running"  # the file is the current state


def test_ledger_and_lock_files_exist(queue, tmp_path):
    ready(queue, "pick vial")
    assert (tmp_path / "queue" / "events.jsonl").exists()
    assert (tmp_path / "queue" / ".lock").exists()


def test_ids_unique(queue):
    items = [queue.submit(title="same", owner="o", est_min=1.0, now=NOON) for _ in range(25)]
    assert len(ids(items)) == 25


def test_get(queue):
    item = ready(queue, "pick vial", blockers=(Blocker(kind="data", why="no labels", eta_t=NOON + 5),),
                 depends_on=(), now=NOON)
    assert queue.get(item.id) == item
    with pytest.raises(KeyError):
        queue.get("no-such-item")


def test_items_filters(queue):
    on_arm1 = ready(queue, "a", robot="arm-1")
    on_any = ready(queue, "b")
    on_arm2 = ready(queue, "c", robot="arm-2")
    cancelled = ready(queue, "d", robot="arm-1")
    queue.cancel(cancelled.id)
    assert ids(queue.items()) == {on_arm1.id, on_any.id, on_arm2.id, cancelled.id}
    assert ids(queue.items(robot="arm-1")) == {on_arm1.id, on_any.id, cancelled.id}
    assert ids(queue.items(status="queued")) == {on_arm1.id, on_any.id, on_arm2.id}
    assert ids(queue.items(status="cancelled")) == {cancelled.id}
    assert ids(queue.items(status="queued", robot="arm-2")) == {on_any.id, on_arm2.id}


def test_a_second_queue_sees_every_change(tmp_path):
    writer, reader = Queue(tmp_path), Queue(tmp_path)
    item = ready(writer, "pick vial", robot="arm-1", now=NOON)
    assert reader.get(item.id) == item
    reader.start(item.id, robot="arm-1", by="pnp-agent", now=NOON + 5)
    assert writer.running("arm-1").id == item.id
    writer.finish(item.id, now=NOON + 65)
    assert reader.get(item.id).status == "done"
    assert len(reader.events()) == len(writer.events()) == 3


# --- readiness ------------------------------------------------------------------------------------------------------

def test_ready_item_has_no_reasons(queue):
    item = ready(queue, "pick vial")
    assert queue.why_not_ready(item.id) == []
    assert queue.is_ready(item.id) is True


def test_why_not_ready_order(queue):
    dep_done = ready(queue, "dep done")
    queue.start(dep_done.id, robot="arm-1", by="o", now=NOON)
    queue.finish(dep_done.id, result="ok", now=NOON + 10)
    dep_open = ready(queue, "dep open")
    dep_failed = ready(queue, "dep failed")
    queue.start(dep_failed.id, robot="arm-2", by="o", now=NOON)
    queue.finish(dep_failed.id, result="error", now=NOON + 10)
    item = queue.submit(title="place bin", owner="ik-agent", est_min=6.0,
                        depends_on=(dep_done.id, dep_open.id, dep_failed.id),
                        blockers=(Blocker(kind="code", why="tests red"), Blocker(kind="human", why="needs a decision")))
    assert queue.why_not_ready(item.id) == [
        "no policy shared",
        "blocked on code: tests red",
        "blocked on human: needs a decision",
        f"waiting on dependency {dep_open.id}",
        f"waiting on dependency {dep_failed.id}",
    ]
    assert queue.is_ready(item.id) is False


def test_status_reason(queue):
    item = ready(queue, "pick vial")
    queue.start(item.id, robot="arm-1", by="o", now=NOON)
    assert queue.why_not_ready(item.id) == ["status running"]
    queue.finish(item.id, now=NOON + 1)
    assert queue.why_not_ready(item.id) == ["status done"]
    failed = ready(queue, "f")
    queue.start(failed.id, robot="arm-1", by="o", now=NOON + 2)
    queue.finish(failed.id, result="timeout", now=NOON + 3)
    assert queue.why_not_ready(failed.id) == ["status failed"]
    cancelled = ready(queue, "c")
    queue.cancel(cancelled.id)
    assert queue.why_not_ready(cancelled.id) == ["status cancelled"]


def test_set_policy_makes_ready(queue):
    item = queue.submit(title="sweep table", owner="sweep-agent", est_min=8.0)
    assert queue.why_not_ready(item.id) == ["no policy shared"]
    updated = queue.set_policy(item.id, policy("sweep"), now=NOON)
    assert updated.policy == policy("sweep")
    assert queue.get(item.id).policy == policy("sweep")
    assert queue.is_ready(item.id)


def test_block_and_unblock(queue):
    item = ready(queue, "place bin")
    updated = queue.block(item.id, "code", "tests red", eta_t=NOON + 600, now=NOON)
    assert updated.blockers == (Blocker(kind="code", why="tests red", eta_t=NOON + 600),)
    queue.block(item.id, "data", "no labels", now=NOON + 1)
    assert queue.why_not_ready(item.id) == ["blocked on code: tests red", "blocked on data: no labels"]
    queue.unblock(item.id, "code", now=NOON + 2)
    assert queue.why_not_ready(item.id) == ["blocked on data: no labels"]
    queue.block(item.id, "hardware", "gripper pad worn", now=NOON + 3)
    queue.unblock(item.id, now=NOON + 4)
    assert queue.get(item.id).blockers == ()
    assert queue.is_ready(item.id)


def test_block_unknown_kind_is_value_error(queue):
    # SPEC: Blocker.kind must be one of BLOCKER_KINDS "else ValueError"; Queue.block builds a Blocker
    item = ready(queue, "place bin")
    before = len(queue.events())
    with pytest.raises(ValueError):
        queue.block(item.id, "weather", "rain")
    assert queue.get(item.id).blockers == ()
    assert len(queue.events()) == before


def test_set_eta(queue):
    item = queue.submit(title="sweep table", owner="sweep-agent", est_min=8.0)
    assert queue.set_eta(item.id, NOON + 3600, now=NOON).eta_t == NOON + 3600
    assert queue.get(item.id).eta_t == NOON + 3600


def test_dependency_ready_only_after_ok_finish(queue):
    first = ready(queue, "calibrate")
    second = ready(queue, "pick vial", depends_on=(first.id,))
    assert queue.why_not_ready(second.id) == [f"waiting on dependency {first.id}"]
    queue.start(first.id, robot="arm-1", by="o", now=NOON)
    assert not queue.is_ready(second.id)
    queue.finish(first.id, result="ok", now=NOON + 60)
    assert queue.is_ready(second.id)


# --- ordering -------------------------------------------------------------------------------------------------------

def test_runnable_order_priority_submitted_id(queue):
    late_agent = ready(queue, "late agent", now=NOON + 50)
    filler = ready(queue, "filler", source="filler", now=NOON - 100)
    system = ready(queue, "system", source="system", now=NOON + 90)
    tie_one = ready(queue, "tie one", now=NOON + 10)
    tie_two = ready(queue, "tie two", now=NOON + 10)
    early_agent = ready(queue, "early agent", now=NOON)
    other_robot = ready(queue, "other robot", robot="arm-2", now=NOON - 500)
    queue.submit(title="not ready", owner="o", est_min=1.0, source="system", now=NOON - 1000)
    expected = [system.id, early_agent.id, *sorted([tie_one.id, tie_two.id]), late_agent.id, filler.id]
    assert [item.id for item in queue.runnable("arm-1")] == expected
    assert [item.id for item in queue.runnable("arm-2")] == [system.id, other_robot.id] + expected[1:]


def test_not_ready_order_and_scope(queue):
    low = queue.submit(title="low", owner="a", est_min=1.0, source="filler", now=NOON - 100)
    high = queue.submit(title="high", owner="b", est_min=1.0, source="system", now=NOON)
    mid = queue.submit(title="mid", owner="c", est_min=1.0, robot="arm-1", now=NOON + 5)
    queue.submit(title="elsewhere", owner="d", est_min=1.0, robot="arm-2", now=NOON)
    ready(queue, "ready one")
    running_one = ready(queue, "running one")
    queue.start(running_one.id, robot="arm-1", by="o", now=NOON)
    assert [item.id for item in queue.not_ready("arm-1")] == [high.id, mid.id, low.id]


# --- start / finish / cancel ----------------------------------------------------------------------------------------

def test_start_records_who_where_when(queue):
    item = ready(queue, "pick vial", executor="rig-runner")
    started = queue.start(item.id, robot="arm-1", by="rig-runner", now=NOON + 7)
    assert started.status == "running"
    assert (started.started_t, started.started_by, started.ran_on) == (NOON + 7, "rig-runner", "arm-1")
    assert queue.running("arm-1") == started
    assert queue.running("arm-2") is None


def test_start_refused_when_not_ready_names_the_reasons(queue):
    item = queue.submit(title="place bin", owner="ik-agent", est_min=6.0, blockers=(Blocker(kind="code", why="tests red"),))
    before = len(queue.events())
    with pytest.raises(QueueError) as refused:
        queue.start(item.id, robot="arm-1", by="ik-agent", now=NOON)
    assert "no policy shared" in str(refused.value)
    assert "blocked on code: tests red" in str(refused.value)
    assert queue.get(item.id).status == "queued"
    assert len(queue.events()) == before


def test_start_refused_when_robot_busy(queue):
    first, second = ready(queue, "first"), ready(queue, "second")
    queue.start(first.id, robot="arm-1", by="o", now=NOON)
    with pytest.raises(QueueError):
        queue.start(second.id, robot="arm-1", by="o", now=NOON + 1)
    assert queue.get(second.id).status == "queued"
    queue.start(second.id, robot="arm-2", by="o", now=NOON + 2)  # another robot is free
    assert queue.running("arm-2").id == second.id


def test_start_refused_on_the_wrong_robot(queue):
    item = ready(queue, "swing", robot="pendulum-1")
    with pytest.raises(QueueError):
        queue.start(item.id, robot="arm-1", by="o", now=NOON)
    assert queue.start(item.id, robot="pendulum-1", by="o", now=NOON).ran_on == "pendulum-1"


def test_finish_ok_is_done(queue):
    item = ready(queue, "pick vial")
    queue.start(item.id, robot="arm-1", by="o", now=NOON)
    done = queue.finish(item.id, note="picked", now=NOON + 90)
    assert (done.status, done.result, done.finished_t, done.note) == ("done", "ok", NOON + 90, "picked")
    assert queue.running("arm-1") is None


def test_finish_anything_else_is_failed(queue):
    item = ready(queue, "pick vial")
    queue.start(item.id, robot="arm-1", by="o", now=NOON)
    failed = queue.finish(item.id, result="timeout", now=NOON + 90)
    assert (failed.status, failed.result) == ("failed", "timeout")


def test_cancel(queue):
    item = ready(queue, "pick vial")
    cancelled = queue.cancel(item.id, note="superseded", now=NOON)
    assert (cancelled.status, cancelled.note) == ("cancelled", "superseded")
    assert queue.runnable("arm-1") == []


# --- snapshot -------------------------------------------------------------------------------------------------------

def test_snapshot(queue):
    running = ready(queue, "running", robot="arm-1", now=NOON - 100)
    queue.start(running.id, robot="arm-1", by="o", now=NOON - 50)
    ready(queue, "ready arm-1", robot="arm-1", now=NOON)
    ready(queue, "ready any", source="filler", now=NOON)
    top = queue.submit(title="blocked", owner="hw-agent", est_min=3.0, robot="arm-1", policy=policy(), priority=2,
                       blockers=(Blocker(kind="hardware", why="gripper pad worn"),), now=NOON)
    queue.submit(title="unshared", owner="sweep-agent", est_min=3.0, robot="arm-1", priority=7, now=NOON)
    ready(queue, "elsewhere", robot="arm-2", now=NOON)
    assert queue.snapshot("arm-1") == QueueSnapshot(
        depth=4, runnable=2, running=running.id,
        top_blocker={"item": top.id, "owner": "hw-agent", "kind": "hardware", "why": "gripper pad worn"})


def test_snapshot_of_an_idle_robot(queue):
    assert queue.snapshot("arm-1") == QueueSnapshot(depth=0, runnable=0, running=None, top_blocker=None)
    unshared = queue.submit(title="unshared", owner="sweep-agent", est_min=3.0, robot="arm-1")
    snapshot = queue.snapshot("arm-1")
    assert (snapshot.depth, snapshot.runnable, snapshot.running) == (1, 0, None)
    # SPEC names {"item","owner","kind","why"} but not what kind/why are for an item with no Blocker (no policy shared);
    # only the item and owner are pinned for that case.
    assert snapshot.top_blocker["item"] == unshared.id
    assert snapshot.top_blocker["owner"] == "sweep-agent"


# --- the events ledger (frozen cross-repo seam, SPEC "The events ledger") ------------------------------------------

def test_ledger_kinds_order_and_times(queue):
    item = ready(queue, "pick vial", robot="arm-1", now=NOON)
    queue.start(item.id, robot="arm-1", by="rig-runner", now=NOON + 5)
    queue.finish(item.id, now=NOON + 65)
    other = queue.submit(title="sweep table", owner="sweep-agent", est_min=8.0, now=NOON + 70)
    queue.set_policy(other.id, policy("sweep"), now=NOON + 75)
    queue.block(other.id, "code", "tests red", now=NOON + 80)
    queue.unblock(other.id, now=NOON + 90)
    queue.set_eta(other.id, NOON + 900, now=NOON + 95)
    queue.cancel(other.id, now=NOON + 100)
    events = queue.events()
    assert [(event["kind"], event["id"]) for event in events] == [
        ("submit", item.id), ("start", item.id), ("finish", item.id), ("submit", other.id), ("policy", other.id),
        ("block", other.id), ("unblock", other.id), ("eta", other.id), ("cancel", other.id)]
    assert [event["t"] for event in events] == [NOON, NOON + 5, NOON + 65, NOON + 70, NOON + 75, NOON + 80, NOON + 90,
                                                NOON + 95, NOON + 100]
    assert all(event["schema"] == EVENT_SCHEMA for event in events)


def test_ledger_file_is_one_json_object_per_line(queue, tmp_path):
    item = ready(queue, "pick vial", now=NOON)
    queue.start(item.id, robot="arm-1", by="o", now=NOON + 1)
    lines = (tmp_path / "queue" / "events.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in lines] == queue.events()
    assert len(lines) == 2


def test_ledger_exact_fields_per_kind(queue, tmp_path):
    """Every event has exactly the base fields plus its kind's extra fields (SPEC tables)."""
    item = queue.submit(title="place bin", owner="ik-agent", est_min=6.0, robot="any", now=NOON)
    submitted_file = json.loads((tmp_path / "queue" / "items" / f"{item.id}.json").read_text())
    queue.set_policy(item.id, policy("place", entrypoint="main"), by="ik-agent", now=NOON + 1)
    queue.block(item.id, "code", "tests red", eta_t=NOON + 600, by="w1-dispatcher", now=NOON + 2)
    queue.block(item.id, "data", "no labels", now=NOON + 3)
    queue.unblock(item.id, "code", by="ik-agent", now=NOON + 4)
    queue.unblock(item.id, now=NOON + 5)
    queue.set_eta(item.id, NOON + 1200, by="ik-agent", now=NOON + 6)
    queue.set_eta(item.id, None, now=NOON + 7)
    queue.start(item.id, robot="arm-1", by="rig-runner", now=NOON + 8)
    queue.finish(item.id, result="timeout", note="stalled at the bin", by="rig-runner", now=NOON + 9)
    cancelled = ready(queue, "sweep", now=NOON + 10)
    queue.cancel(cancelled.id, now=NOON + 11)
    events = queue.events()
    by_kind = [event["kind"] for event in events]
    assert by_kind == ["submit", "policy", "block", "block", "unblock", "unblock", "eta", "eta", "start", "finish",
                       "submit", "cancel"]
    submit, set_policy, block_code, block_data, unblock_code, unblock_all, eta_set, eta_clear, start, finish, _, cancel = events

    assert set(submit) == BASE_KEYS | {"item"}
    assert submit["item"] == submitted_file  # the full Item as a dict, as in its JSON file
    assert submit["item"]["id"] == item.id
    assert (submit["robot"], submit["by"]) == ("any", "ik-agent")  # submit's by defaults to the owner

    assert set(set_policy) == BASE_KEYS | {"policy"}
    assert {key: set_policy["policy"][key] for key in ("repo", "commit", "path", "entrypoint")} == {
        "repo": "https://github.com/example/place-policies", "commit": "0123456789abcdef0123456789abcdef01234567",
        "path": "policies/place.py", "entrypoint": "main"}
    assert set_policy["policy"].get("sha256") is None and set_policy["policy"].get("description") is None
    assert set_policy["by"] == "ik-agent"

    assert set(block_code) == BASE_KEYS | {"blocker"}
    assert block_code["blocker"] == {"kind": "code", "why": "tests red", "eta_t": NOON + 600}
    assert block_code["by"] == "w1-dispatcher"
    assert block_data["blocker"] == {"kind": "data", "why": "no labels", "eta_t": None}
    assert block_data["by"] is None  # by is optional and recorded as null

    assert set(unblock_code) == BASE_KEYS | {"blocker_kind", "cleared"}
    assert unblock_code["blocker_kind"] == "code"
    assert unblock_code["cleared"] == [{"kind": "code", "why": "tests red", "eta_t": NOON + 600}]
    assert unblock_all["blocker_kind"] is None
    assert unblock_all["cleared"] == [{"kind": "data", "why": "no labels", "eta_t": None}]

    assert set(eta_set) == BASE_KEYS | {"eta_t"}
    assert eta_set["eta_t"] == NOON + 1200
    assert eta_clear["eta_t"] is None

    assert set(start) == BASE_KEYS
    assert (start["robot"], start["by"]) == ("arm-1", "rig-runner")  # the robot it runs on, not "any"

    assert set(finish) == BASE_KEYS | {"result", "note"}
    assert (finish["robot"], finish["result"], finish["note"], finish["by"]) == (
        "arm-1", "timeout", "stalled at the bin", "rig-runner")

    assert set(cancel) == BASE_KEYS | {"note"}
    assert (cancel["robot"], cancel["note"], cancel["by"]) == ("any", None, None)

    for event in events:
        assert event["schema"] == EVENT_SCHEMA
        assert isinstance(event["t"], float)


def test_ledger_robot_before_and_after_start(queue):
    item = ready(queue, "pick vial", now=NOON)  # robot "any"
    queue.start(item.id, robot="arm-2", by="o", now=NOON + 1)
    queue.cancel(item.id, note="operator stop", by="zach", now=NOON + 2)  # cancel of a started item
    submit, start, cancel = queue.events()
    assert submit["robot"] == "any"
    assert start["robot"] == "arm-2"
    assert (cancel["robot"], cancel["note"], cancel["by"]) == ("arm-2", "operator stop", "zach")
    assert set(cancel) == BASE_KEYS | {"note"}


def test_submit_by_overrides_the_owner_default(queue):
    item = queue.submit(title="self-test", owner="rig-owner", est_min=2.0, by="activity-agent", now=NOON)
    assert queue.events()[0]["by"] == "activity-agent"
    assert queue.get(item.id).owner == "rig-owner"


def test_finish_ok_event(queue):
    item = ready(queue, "pick vial")
    queue.start(item.id, robot="arm-1", by="o", now=NOON)
    queue.finish(item.id, now=NOON + 30)
    finish = queue.events()[-1]
    assert (finish["kind"], finish["result"], finish["note"], finish["by"]) == ("finish", "ok", None, None)


# --- concurrency ----------------------------------------------------------------------------------------------------

def test_concurrent_submits_from_two_processes_all_land(tmp_path):
    per_worker, workers = 30, 2
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    processes = [context.Process(target=submit_many, args=(str(tmp_path), f"worker{index}", per_worker, start_event))
                 for index in range(workers)]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(180)
        assert process.exitcode == 0
    queue = Queue(tmp_path)
    items = queue.items()
    assert len(items) == per_worker * workers
    assert len(ids(items)) == per_worker * workers
    assert sorted(item.title for item in items) == sorted(f"worker{w}-{i}" for w in range(workers) for i in range(per_worker))
    submits = [event for event in queue.events() if event["kind"] == "submit"]
    assert len(submits) == per_worker * workers
    assert {event["id"] for event in submits} == ids(items)
    for item in items:
        assert (tmp_path / "queue" / "items" / f"{item.id}.json").exists()
