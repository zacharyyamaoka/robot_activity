# Robot Activity Agent — its own to-do list

Open items first; the AI-team status file reads the first three. Format: `- [ ] text`, or `- [x] text (done YYYY-MM-DD)`.

## Open

- [ ] Escalate `start_next` to the dispatcher after 3 unanswered prompts instead of re-prompting the executor every 15 min (demo day: 79 prompts to one executor on arm-2)
- [ ] Move BAM's in-house monitor onto this core: native robot-activity/1 rows from its sampler and its queue events as robot-activity-event/1, then retire the adapter
- [ ] Split activity hours by source (system / agent / filler), so filler work never passes for useful work
- [ ] Deliver prompts without a model call: a direct notifier for agent sessions (each BAM page is a `claude -p` relay run today)
- [ ] Show the throughput counterweight (real picks per day) beside utilisation on the dashboard

## Done

- [x] v0.1.0: the contract, 221 acceptance tests, the dashboard, W&B, the public repo and landing page (done 2026-11-18)
- [x] ETA asks once per answer: skip owners with a future ETA, one ask per item per 15 min (done 2026-11-18)
- [x] BAM's idle router back on after the 11-17 overnight budget freeze (done 2026-11-18)
