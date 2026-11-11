# A prompt checkpoint does not outwait Claude

Date: 2026-10-01. Preserve the existing foreground hook and durable ingress.

The owner reports Claude's UserPromptSubmit hook timing out after its installed
5 seconds. The shipped and installed settings both select integration_adapter.py
with user_prompt_capture.py. Its current direct-ingress dispatch does not call the
old bounded delegate. After accepting complete durable input, it synchronously
observes a project checkpoint. Default Markdown writer acquisition may wait
10 seconds. The old delegate budget tests do not govern this direct path.

A real adopted-vault regression starts another fenced Markdown writer only after
the prompt's durable publication completes. The actual ingress then returns after
10.16686 seconds, exceeding the existing 5-second host contract. This reproduces
one cause of the reported message; another agent's precise occurrence, import
startup, filesystem latency and scheduling were not traced, so it is not evidence
that every timeout has this single cause.

For UserPromptSubmit's checkpoint follow-up, use the existing writer API's zero
wait option: attempt admission once. An available writer still commits the normal
checkpoint. A busy writer leaves the already persisted ordered pending checkpoint
and its recovery evidence for the next existing actor. The actual test releases
the writer and proves unattended draining completes the same pending work and
journal. No new process, daemon, tool, directory, timer or setting is introduced.
SessionStart retains its recovery allowance; other event policies are unchanged.
Zero means nonblocking admission, not a new source quota or truncated capture.

Sources checked 2026-10-01:

- [Claude hooks reference](https://code.claude.com/docs/en/hooks): UserPromptSubmit runs before prompt processing and command handlers have explicit timeouts.
- [SQLite isolation](https://www.sqlite.org/isolation.html): concurrent local writers still require coordination; changing a host's wait does not remove that contention.
- [Python 3.10 monotonic clock](https://docs.python.org/3.10/library/time.html#time.monotonic): measure elapsed caller budgets without wall-clock adjustments.
- [AWS transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html): retain pending work and reuse idempotent delivery rather than claim completion prematurely.

Alternatives: raising the host timeout would lengthen every stalled prompt without
fixing this inappropriate foreground writer wait; deleting or dropping pending
checkpoints loses project evidence; a detached replacement hook cannot guarantee
timely foreground advisory context and would change the host flow. Existing
nonblocking admission and recovery require no parallel implementation or migrated
state. They remove this writer wait, not all OS or scheduler latency: this is not
a universal five-second completion guarantee for the entire hook.

The red case fails on the original 10-second wait. Initial related prompt and
breadcrumb checks pass 45 tests. Both contended and uncontended real scenarios,
and five stale-publication controls, pass 7 tests. Their whole-call durations are
recorded, including later recovery; they must not be presented as isolated prompt
latencies. Complete current-source and all-platform CI success remain unclaimed.

Final related qualification: 298 passed / 21 skipped, including actual project journal, durable ingress, publication recovery, complete source/branch/CCN guards and existing hook-budget controls. Changed/new functions independently measured with real Lizard: maximum CCN 4. Initial Ruff import-order failure was corrected without changing checks. Later installed/publication evidence is retained separately.
