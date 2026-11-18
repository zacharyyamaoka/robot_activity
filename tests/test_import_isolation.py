"""Runtime dependencies are robot-msgs + the stdlib: importing every module leaves wandb, matplotlib and rerun (and any
lab's private code) out of sys.modules (SPEC "Package layout")."""

from __future__ import annotations

import json
import sys

from procs import run_python

MODULES = ("robot_activity", "robot_activity.schema", "robot_activity.classify", "robot_activity.motion",
           "robot_activity.store", "robot_activity.queue", "robot_activity.metrics", "robot_activity.stalemate",
           "robot_activity.agent", "robot_activity.notify", "robot_activity.sampler", "robot_activity.views",
           "robot_activity.views.terminal", "robot_activity.views.html", "robot_activity.wandb_log",
           "robot_activity.demo", "robot_activity.cli")
FORBIDDEN = ("wandb", "matplotlib", "rerun", "robot_metrics", "sim2real", "robot_models")

TOP_LEVEL = """
import json, sys
{imports}
print(json.dumps(sorted({{name.split(".")[0] for name in sys.modules}})))
"""


def top_level_modules(imports: list[str], fake_heavy_modules) -> set[str]:
    result = run_python(TOP_LEVEL.format(imports="\n".join(f"import {name}" for name in imports)),
                        first_on_path=fake_heavy_modules)
    assert result.returncode == 0, result.stderr
    return set(json.loads(result.stdout))


def test_no_heavy_or_private_imports(fake_heavy_modules):
    loaded = top_level_modules(list(MODULES), fake_heavy_modules)
    assert not loaded & set(FORBIDDEN), loaded & set(FORBIDDEN)


def test_nothing_beyond_robot_msgs_and_the_stdlib(fake_heavy_modules):
    baseline = top_level_modules(["robot_msgs", "robot_msgs.sensor"], fake_heavy_modules)  # robot-msgs and its own deps
    loaded = top_level_modules(list(MODULES), fake_heavy_modules)
    extra = {name for name in loaded - baseline - {"robot_activity"}
             if name not in sys.stdlib_module_names and not name.startswith("_")}
    assert extra == set(), extra
