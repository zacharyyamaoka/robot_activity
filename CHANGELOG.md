# Changelog

## 0.1.1 · 2026-11-18

- **The quickstart works as written.** `robot-activity demo` for today stops at the present
  (`simulate_fleet(..., until=)`), so `queue` right after it never shows a negative elapsed time; the README's
  `agent tick` no longer points at `examples/agent.toml`, which a `pip install` does not ship. 224 tests.

## 0.1.0 · 2026-11-18

The first release of the Robot Activity Agent: keep every robot doing useful work, and say why when it isn't.

- **The contract** (`docs/SPEC.md`) and 221 acceptance tests written from it before the code, including a parity test
  against one lab's in-house monitor on a real arm (agreement within 0.01 h).
- **Metrics**: three clean splits of tracked time (claimed/unclaimed, activity/dead, moving/still/unknown), a reason
  for every dead or still hour, owners, streaks, hand-off gaps, fleet roll-ups and stalemate flags (stuck, looping,
  overrun).
- **The queue**: one JSON file per item, an append-only events ledger (`robot-activity-event/1`, frozen) and a lock.
  Owners and executors; ready only once a `PolicyRef` is shared; priority system > agent > filler; one running item per
  robot; replay to any moment.
- **The agent**: one loop that prompts executors, asks owners for ETAs once per answer, enqueues the program's declared
  fillers and flags stalemates; its own KPI is idle unprompted. Notifiers: a JSON-lines outbox or any command.
- **Views**: the one-screen terminal queue and a one-file HTML dashboard (light and dark, phone-width safe, no network).
- **W&B** (extra): one run per robot per day, one step per minute, summary hours and tables, a fleet run, a saved Report.
- **Drop-in**: `AGENT.md`, a Claude Code subagent definition, `examples/agent.toml`, a simulated demo fleet day, and
  the `robot-activity` command.
- **The landing page**: https://zacharyyamaoka.github.io/robot_activity/
