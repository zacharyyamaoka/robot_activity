"""Samples on disk, as files a human can read: `<root>/samples/<robot>/<YYYY-MM-DD>.jsonl`, one row per line.

The date in the file name is the row's `t` in the store's time zone (None = system local time). Lines that do not parse
(a crash mid-write, a stray edit) are skipped, never fatal.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from robot_activity.schema import Sample


def check_name(name: str, what: str = "name") -> str:
    """A robot or item name is used as a file name: refuse separators and hidden-file names."""
    if not name or "/" in name or "\\" in name or name.startswith(".") or "\x00" in name:
        raise ValueError(f"invalid {what}: {name!r}")
    return name


class Store:
    def __init__(self, root: str | Path, tz: str | None = None):
        self.root = Path(root)
        self.tz = tz
        self.zone: datetime.tzinfo | None = ZoneInfo(tz) if tz else None

    # --- time -------------------------------------------------------------------------------------------------------

    def local(self, t: float) -> datetime.datetime:
        """`t` as a datetime in the store's zone (naive system-local time when the store has no zone)."""
        return datetime.datetime.fromtimestamp(t, self.zone) if self.zone else datetime.datetime.fromtimestamp(t)

    def day_of(self, t: float) -> str:
        return self.local(t).date().isoformat()

    def day_bounds(self, day: str) -> tuple[float, float]:
        """Unix seconds of the day's first and next day's first instant, in the store's zone."""
        start = datetime.date.fromisoformat(day)
        end = start + datetime.timedelta(days=1)
        return (datetime.datetime(start.year, start.month, start.day, tzinfo=self.zone).timestamp(),
                datetime.datetime(end.year, end.month, end.day, tzinfo=self.zone).timestamp())

    # --- write ------------------------------------------------------------------------------------------------------

    def path(self, robot: str, day: str) -> Path:
        return self.root / "samples" / check_name(robot, "robot") / f"{day}.jsonl"

    def append(self, sample: Sample) -> Path:
        path = self.path(sample.robot, self.day_of(sample.t))
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(sample.to_dict()) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)  # one write per line: concurrent appenders never interleave inside a row
        return path

    # --- read -------------------------------------------------------------------------------------------------------

    def rows(self, robot: str, day: str) -> list[Sample]:
        path = self.path(robot, day)
        if not path.is_file():
            return []
        samples = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                samples.append(Sample.from_dict(json.loads(line)))
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        samples.sort(key=lambda sample: sample.t)
        return samples

    def since(self, robot: str, since_t: float, until_t: float) -> list[Sample]:
        """Rows with since_t < t <= until_t, across day files."""
        first_day, last_day = self.day_of(since_t), self.day_of(until_t)
        rows = []
        for day in self.days(robot):
            if first_day <= day <= last_day:
                rows.extend(row for row in self.rows(robot, day) if since_t < row.t <= until_t)
        rows.sort(key=lambda sample: sample.t)
        return rows

    def robots(self) -> list[str]:
        folder = self.root / "samples"
        if not folder.is_dir():
            return []
        return sorted(entry.name for entry in folder.iterdir() if entry.is_dir() and not entry.name.startswith("."))

    def days(self, robot: str) -> list[str]:
        folder = self.root / "samples" / check_name(robot, "robot")
        if not folder.is_dir():
            return []
        return sorted(entry.stem for entry in folder.glob("*.jsonl"))

    def latest(self, robot: str) -> Sample | None:
        for day in reversed(self.days(robot)):
            rows = self.rows(robot, day)
            if rows:
                return rows[-1]
        return None
