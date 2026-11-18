"""Fixtures shared by the acceptance tests.

This file never imports robot_activity, so pytest can load it (and collect every test file) before the package exists.
Builders that need the package live in `helpers.py`; subprocess helpers live in `procs.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def fake_heavy_modules(tmp_path: Path) -> Path:
    """A folder of importable stand-ins for wandb, matplotlib and rerun.

    Put it first on a subprocess's PYTHONPATH: any import of those names then succeeds and shows up in sys.modules, so a
    test can tell "never imported" from "imported but not installed here" (the real libraries may be absent).
    """
    folder = tmp_path / "fake_site"
    for name in ("wandb", "matplotlib", "rerun"):
        package = folder / name
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(f"STAND_IN = {name!r}\n")
    return folder
