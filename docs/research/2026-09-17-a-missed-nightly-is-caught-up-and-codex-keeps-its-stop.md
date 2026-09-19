# A missed nightly is caught up, and Codex keeps its Stop

Dated 2026-09-17. Two decisions the third audit left to the owner and the owner delegated back:
finding C-F5 (the nightly catch-up no shipped hook can reach) and the `SessionEnd` registration
the Codex fix of this morning left open.

## What was found — the catch-up

- `session_start_context._maybe_spawn_nightly_catchup` claims today's catch-up in `run/state.json`
  and spawns `scheduled_nightly.py` when no nightly completed today. It is called only from that
  module's `main()`, which the shipped Claude and Codex hooks never reach: they go through
  `integration_adapter`, which imports `build_context_items` alone.
- The audit's first round recommended deleting it, because the schedulers catch up themselves.
  Checked, per scheduler, against what this product installs:
  - Linux, user systemd timer: `install_control.py` writes `Persistent=true`, and systemd's own
    definition is "the time when the service unit was last triggered is stored on disk. When the
    timer is activated, the service unit is triggered immediately if it would have been triggered
    at least once during the time when the timer was inactive… This is useful to catch up on
    missed runs of the service when the system was powered down"
    ([systemd.timer(5)](https://man7.org/linux/man-pages/man5/systemd.timer.5.html), fetched
    2026-09-17). Covered.
  - macOS LaunchAgent, `StartCalendarInterval`: "Unlike cron which skips job invocations when the
    computer is asleep, launchd will start the job the next time the computer wakes up. If multiple
    intervals transpire before the computer is woken, those events will be coalesced into one event
    upon wake from sleep" ([launchd.plist(5)](https://keith.github.io/xcode-man-pages/launchd.plist.5.html),
    fetched 2026-09-17). Covered while the user is logged in.
  - Windows Task Scheduler: `install-scheduled-tasks.ps1` passes `-StartWhenAvailable`, which
    "indicates that the Task Scheduler can start the task at any time after its scheduled time has
    passed", queued "after a delay. The default delay is 10 minutes"
    ([TaskSettings.StartWhenAvailable](https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-startwhenavailable),
    fetched 2026-09-17). Covered after the user signs in; our own `docs/USER-GUIDE.md` already says
    these tasks "run only while the current user is logged on".
  - cron, the explicit degraded fallback: cron has no catch-up at all, as the launchd page says of
    it above.
- So the recommendation is right for three of the four, and wrong for the fourth — and wrong for
  every machine that is asleep or signed out at 03:00 night after night, which is the scenario the
  audit named.

## What was found — Codex `SessionEnd`

- This morning's fix stopped treating a Codex `Stop` as the end of a session: a session is captured
  on its first turn and then only after 30 quiet minutes, picked up by whatever Codex hook runs
  next (`docs/research/2026-09-17-a-codex-turn-is-not-a-session.md`). That note left the question of
  registering the host's own `SessionEnd` to the owner.
- The host's terms, fetched again today ([Codex hooks](https://learn.chatgpt.com/docs/hooks),
  2026-09-17): `SessionEnd` "runs when a session ends… for the main thread when you archive or
  delete an open conversation, when Codex closes normally, or after 30 minutes of inactivity
  without any connected clients". Its budget is the catch: a 1 second default timeout with a
  maximum of 3 seconds, where other hooks default to 600, and hooks always run synchronously.
- Our lifecycle entry point is `uv run … python scripts/codex_memory.py hook`: a Python start, a
  transcript read and a durable publication under a fence. The shipped budget for it everywhere
  else is 15 seconds. A 3 second ceiling cannot hold it, and a hook killed at the ceiling is a
  publication cut in half.

## The decisions

- **The catch-up is wired in, not deleted.** `integration_adapter._run_session_start_maintenance`
  — the detached pass that already runs the capture worker, the queue worker and the compile on
  every session start — asks for it. Nothing is added to the hook's own budget, the claim in
  `run/state.json` still allows exactly one catch-up per day, and `MEMORY_LLM_PROVIDER=fake` still
  disables it for tests. `docs/USER-GUIDE.md` gains the one sentence that says so, because the
  guide currently tells the reader that a missed nightly depends on the operating system alone.
- **Codex `SessionEnd` is not registered.** A capture cannot be promised inside a 3 second
  synchronous ceiling, and the tail of a Codex session is already captured by the next hook after
  the same 30 minutes of quiet that the host itself uses to fire the event. What would change this:
  a signal cheap enough for that ceiling — a marker the next pass reads — measured on a real Codex
  install. No Codex is installed on this machine, so that measurement is not available here.

Files: `scripts/integration_adapter.py`, `scripts/session_start_context.py`,
`docs/USER-GUIDE.md`, `tests/test_a_missed_nightly_is_caught_up.py`
