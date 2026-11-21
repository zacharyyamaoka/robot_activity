"""wandb_log.push sends W&B summary statistics only (SPEC "wandb_log.py"; Zach, 2026-11-21: "to help limit W and B storage I think we
need to just be using it for summary statistics; for the media-rich things we should be using rerun!").

push runs against a stand-in `wandb` whose Table (and every media type) raises: the steps and summary are scalars, each run's tables
land as jsonl in <root>/tables/<day>/<run id>/, and the summary names that folder. A second push logs no new step. No network.
"""

from __future__ import annotations

import json
import sys
import types

from helpers import DAY
from robot_activity import wandb_log
from test_wandb_payload import build


def refuse(*args, **kwargs):
    raise AssertionError("summary-only: a media or table object was built for W&B")


class Run:
    def __init__(self, kwargs, store):
        self.kwargs, self.logs, self.summary, self.url = kwargs, [], {}, f"https://example/e/p/runs/{kwargs['id']}"
        store[kwargs["id"]] = self

    def define_metric(self, *args, **kwargs):
        pass

    def log(self, record, step=None):
        self.logs.append(record)

    def finish(self):
        pass


def test_push_sends_scalars_and_keeps_the_tables_beside_the_samples(tmp_path, monkeypatch):
    root = tmp_path / "root"
    store, queue = build(root)
    runs: dict[str, Run] = {}
    summaries: dict[str, dict] = {}
    api = types.SimpleNamespace(default_entity="e",
                                run=lambda path: types.SimpleNamespace(summary=summaries[path.rsplit("/", 1)[-1]]))
    fake = types.SimpleNamespace(Api=lambda: api, Settings=lambda **kwargs: kwargs,
                                 init=lambda **kwargs: Run(kwargs, runs), **{name: refuse for name in (
                                     "Table", "Image", "Video", "Html", "Plotly", "Histogram", "Object3D", "Artifact")})
    monkeypatch.setitem(sys.modules, "wandb", fake)
    result = wandb_log.push(store, queue, DAY, "p")
    assert {entry["run_id"] for entry in result["runs"]} == {f"arm-1-{DAY}", f"pendulum-1-{DAY}", f"fleet-{DAY}"}
    for run_id, run in runs.items():
        assert all(isinstance(v, (int, float, str, bool, type(None))) for record in run.logs for v in record.values())
        assert all(isinstance(v, (int, float, str, bool, type(None), dict, list)) for v in run.summary.values())
        folder = root / "tables" / DAY / run_id
        assert run.summary["local_tables"] == str(folder)
        for name in wandb_log.payload(store, queue, "arm-1", DAY)["tables"] if run_id.startswith("arm-1") else ():
            rows = [json.loads(line) for line in (folder / f"{name}.jsonl").read_text().splitlines()]
            assert len(rows) == run.summary[f"table/{name}_rows"]
        summaries[run_id] = dict(run.summary)
    first = {run_id: len(run.logs) for run_id, run in runs.items()}
    runs.clear()
    again = wandb_log.push(store, queue, DAY, "p")
    assert all(entry["logged"] == 0 for entry in again["runs"]) and first
