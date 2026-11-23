"""Robot Activity Agent: keep every robot doing useful work, and say why when it isn't.

The package measures activity per robot and per fleet on three clean splits of tracked time (claimed / unclaimed,
activity / dead, moving / still / unknown), keeps a work queue whose items belong to their owners, flags stalemates,
and runs an agent loop that prompts, asks for ETAs and enqueues the program's own declared fillers. The contract is
`docs/SPEC.md`.

Runtime dependencies are robot-msgs and the standard library; `wandb` is an extra imported only by
`robot_activity.wandb_log.push`.
"""

from robot_activity.classify import Classified, classify
from robot_activity.queue import BLOCKER_KINDS, DEFAULT_PRIORITY, Blocker, Item, PolicyRef, Queue, QueueError
from robot_activity.schema import SCHEMA, QueueSnapshot, Sample
from robot_activity.store import Store

__version__ = "0.1.1"

__all__ = [
    "BLOCKER_KINDS",
    "DEFAULT_PRIORITY",
    "SCHEMA",
    "Blocker",
    "Classified",
    "Item",
    "PolicyRef",
    "Queue",
    "QueueError",
    "QueueSnapshot",
    "Sample",
    "Store",
    "__version__",
    "classify",
]
