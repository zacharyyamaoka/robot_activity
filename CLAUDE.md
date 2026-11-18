# robot_activity — working notes for agents

The Robot Activity Agent: keep every robot doing useful work, and say why when it isn't. The contract is
`docs/SPEC.md`; read it before changing anything. Change the spec first, then the tests, then the code.

## Rules that are Zach's (keep the `WHY:` comments that carry them)

- The agent is in charge of activity, never of WHAT the work is. It prompts, asks for ETAs, enqueues the program's own
  declared fillers and flags stalemates. It never writes job content and never starts a job itself.
- An item is ready only once its owner has shared the code/policy it will run (`PolicyRef`), so a dispatcher that sees a
  stalemate can read the code and judge whether the behaviour is expected.
- Hours moving is a proxy, not an objective (Goodhart, Theory of Constraints). Keep the caveat on the page.
- Three clean splits of tracked time: claimed/unclaimed, activity/dead, moving/still/unknown. Unknown is never idle.
- SI inside; degrees only at display.

## Layering

- Runtime deps: robot-msgs (PyPI) + stdlib. wandb is an extra imported only in `wandb_log.py`.
- Never import robot_metrics, sim2real, robot_models or any lab's private code.
- Files a human can read are the state: samples JSONL, one JSON per queue item, an append-only events ledger.

## Tests

```bash
cd ~/robot_activity && taskset -c 2-5,16,17 uv run --extra test pytest -q
```

- The acceptance tests were committed before the implementation. Don't edit a green test except to add.
- `archive` tests read one lab's real rows and skip cleanly without them.

## Machine

- At most 2 worker processes, pinned `taskset -c 2-5,16,17`; never CPUs 22-23. Never touch /dev/ttyACM*, CAN, cameras.
- Public: github.com/zacharyyamaoka/robot_activity (MIT). Never commit data, recordings, private paths or secrets.
- Landing page: `website/build_site.py` then `website/publish_pages.py` (gh-pages).
- Reports: `reports/<name>-<date>.html`.
