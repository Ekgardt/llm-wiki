# A watch nothing schedules is not daytime freshness

Dated 2026-09-18. Third audit, finding D-2 (`scripts/freshness_watch.py` is scheduled by
nothing). The research before the decision.

Files: `scripts/freshness_watch.py` (removed), `tests/test_freshness_watch.py` (removed),
`scripts/corpus_snapshot.py` (the probe only it used), `scripts/mcp_server.py` (one comment).

## What was found

- `scripts/freshness_watch.py` (587 lines, 368 lines of tests) is complete, bounded and
  measured: a tier-0 identity probe of 0.078 s, a tier-1 snapshot of 1.21 s, and the same
  fenced builder the nightly pass uses. It closes a real window — measured by its own author
  on 2026-08-28 at 17:05, 84 of the vault's 821 sources differed from the active generation.
- Nothing runs it. The scheduler definitions in `install_control.py` name two kinds,
  `nightly` and `weekly`; no hook, no MCP tool, no script and no CI job imports it. The only
  claim that it runs is a comment in `mcp_server._refresh_action` ("the nightly pass and the
  freshness watch").
- No contract promises it: it is absent from `CLAUDE.md`, `AGENTS.md`, `docs/STRUCTURE.md`,
  `docs/USER-GUIDE.md` and all three READMEs. Only two research notes mention it in passing.
- Wiring it is not a small change. A third scheduled kind has to be rendered for four
  backends (user systemd timer, launchd agent, Windows Task Scheduler, cron), and on Windows
  the task specification is versioned and validated against an exact expected task list
  (`WINDOWS_TASK_SPEC_VERSION = 2`, `_windows_spec_version`), so a third task is a
  specification version bump with a migration for already-installed machines — the exact
  work `docs/research/2026-09-17-a-changed-task-setting-reaches-an-installed-machine.md`
  describes for version 2. The install manifests, the uninstall path, the verify path and
  their tests all carry the same list.

## Practice on this date

- systemd.timer: "OnCalendar= — defines realtime (i.e. wallclock) timers with calendar event
  expressions… the service is activated each time the timer elapses"
  (https://www.freedesktop.org/software/systemd/man/systemd.timer.html, fetched 2026-09-18).
  An hourly daytime timer is a five-line unit — the cost is not the unit, it is the four
  backends and the versioned Windows specification that must agree about the same list.
- The owner's standing rule for this round: a feature nothing runs is wired in when a
  contract promises it and the wiring is small, and deleted otherwise.

## The decision

Delete it. No contract promises it, nothing schedules it, and the wiring is a scheduler
specification change across four backends with an installed-machine migration — larger than
the feature, and in an area another pass is editing.

What this leaves open, stated plainly so it can be asked for deliberately: between one
nightly build and the next, the vault's memory generation does not contain what was written
that day; retrieval falls back to the legacy index for those sources. Closing that window
needs a third scheduled kind, and that is a decision about the scheduler contract, not about
this module. The design, the three tiers and the measurements stay in git history and in
`docs/research/2026-08-28-*`.

Two things go with it: `corpus_snapshot.probe_corpus_identity` and its `CorpusProbe` row
helpers, whose only caller it was, and the `mcp_server` comment that told the reader the
watch was running.
