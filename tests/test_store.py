"""store.py: samples as JSONL files a human can read, one file per robot per local day (SPEC "store.py")."""

from __future__ import annotations

import datetime
import json
from zoneinfo import ZoneInfo

from helpers import sample
from robot_activity.schema import Sample
from robot_activity.store import Store

UTC = datetime.timezone.utc
UTC_MIDNIGHT = datetime.datetime(2026, 11, 18, 0, 0, 0, tzinfo=UTC).timestamp()
# 2026-11-18 00:00 in Vancouver, from the zone rules themselves (UTC-7 or UTC-8 depending on the tzdata release: BC's
# permanent daylight time is in tzdata 2026e), so the boundary tests hold on any machine
VANCOUVER_MIDNIGHT = datetime.datetime(2026, 11, 18, tzinfo=ZoneInfo("America/Vancouver")).timestamp()


def test_day_of_utc_around_midnight(tmp_path):
    store = Store(tmp_path, tz="UTC")
    assert store.day_of(UTC_MIDNIGHT - 0.001) == "2026-11-17"
    assert store.day_of(UTC_MIDNIGHT) == "2026-11-18"
    assert store.day_of(VANCOUVER_MIDNIGHT - 1.0) == "2026-11-18"


def test_day_of_vancouver_around_midnight(tmp_path):
    store = Store(tmp_path, tz="America/Vancouver")
    assert store.day_of(VANCOUVER_MIDNIGHT - 1.0) == "2026-11-17"
    assert store.day_of(VANCOUVER_MIDNIGHT) == "2026-11-18"
    assert store.day_of(UTC_MIDNIGHT) == "2026-11-17"


def test_day_of_default_is_system_local_time(tmp_path):
    store = Store(tmp_path)
    for t in (UTC_MIDNIGHT - 1.0, UTC_MIDNIGHT, VANCOUVER_MIDNIGHT - 1.0, VANCOUVER_MIDNIGHT):
        assert store.day_of(t) == datetime.datetime.fromtimestamp(t).date().isoformat()


def test_append_writes_one_line_to_the_day_file(tmp_path):
    store = Store(str(tmp_path), tz="America/Vancouver")
    late = sample(VANCOUVER_MIDNIGHT - 1.0, robot="arm-1", claimed_by="alice", moving=True)
    early = sample(VANCOUVER_MIDNIGHT, robot="arm-1", moving=False)
    late_path = store.append(late)
    early_path = store.append(early)
    assert late_path == tmp_path / "samples" / "arm-1" / "2026-11-17.jsonl"
    assert early_path == tmp_path / "samples" / "arm-1" / "2026-11-18.jsonl"
    lines = late_path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["schema"] == "robot-activity/1"
    assert Sample.from_dict(json.loads(lines[0])) == late


def test_same_t_lands_on_different_days_in_different_zones(tmp_path):
    row = sample(VANCOUVER_MIDNIGHT - 1.0)
    assert Store(tmp_path / "utc", tz="UTC").append(row).name == "2026-11-18.jsonl"
    assert Store(tmp_path / "van", tz="America/Vancouver").append(row).name == "2026-11-17.jsonl"


def test_rows_sorted_by_t(tmp_path):
    store = Store(tmp_path, tz="UTC")
    for t in (UTC_MIDNIGHT + 30, UTC_MIDNIGHT + 10, UTC_MIDNIGHT + 20):
        store.append(sample(t))
    assert [row.t for row in store.rows("arm-1", "2026-11-18")] == [UTC_MIDNIGHT + 10, UTC_MIDNIGHT + 20, UTC_MIDNIGHT + 30]


def test_rows_skip_unparsable_lines(tmp_path):
    store = Store(tmp_path, tz="UTC")
    first = sample(UTC_MIDNIGHT + 10, claimed_by="alice")
    path = store.append(first)
    with path.open("a") as handle:
        handle.write("not json at all\n")
        handle.write('{"schema": "robot-activity/1", "t": 17949\n')  # a line cut off by a crash
        handle.write("\n")
    second = sample(UTC_MIDNIGHT + 20, claimed_by="bob")
    store.append(second)
    assert store.rows("arm-1", "2026-11-18") == [first, second]


def test_rows_of_unknown_robot_or_day_are_empty(tmp_path):
    store = Store(tmp_path, tz="UTC")
    store.append(sample(UTC_MIDNIGHT + 10))
    assert store.rows("arm-9", "2026-11-18") == []
    assert store.rows("arm-1", "2026-11-19") == []


def test_robots_and_days_sorted(tmp_path):
    store = Store(tmp_path, tz="UTC")
    assert store.robots() == []
    store.append(sample(UTC_MIDNIGHT + 86400 + 5, robot="zeta"))
    store.append(sample(UTC_MIDNIGHT + 5, robot="zeta"))
    store.append(sample(UTC_MIDNIGHT - 5, robot="alpha"))
    assert store.robots() == ["alpha", "zeta"]
    assert store.days("zeta") == ["2026-11-18", "2026-11-19"]
    assert store.days("alpha") == ["2026-11-17"]
    assert store.days("nobody") == []


def test_latest_across_days(tmp_path):
    store = Store(tmp_path, tz="UTC")
    assert store.latest("arm-1") is None
    newest = sample(UTC_MIDNIGHT + 86400 + 50, claimed_by="carol")
    store.append(newest)
    store.append(sample(UTC_MIDNIGHT + 86400 + 40))
    store.append(sample(UTC_MIDNIGHT + 100))
    assert store.latest("arm-1") == newest


def test_a_second_store_reads_what_the_first_wrote(tmp_path):
    Store(tmp_path, tz="UTC").append(sample(UTC_MIDNIGHT + 10, claimed_by="alice"))
    assert [row.claimed_by for row in Store(tmp_path, tz="UTC").rows("arm-1", "2026-11-18")] == ["alice"]
