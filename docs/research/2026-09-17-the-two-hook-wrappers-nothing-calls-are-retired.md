# The two hook wrappers nothing calls are retired

Dated 2026-09-17. The third audit's dead-code row for the capture area: `precompact_capture.py`
and `session_end_capture.py`, "near-identical twins" whose detached flush path no shipped hook
reaches. The owner delegated the keep-or-retire decision.

## What was found

- Both scripts do one thing: read a hook payload from stdin and spawn `flush_memory.py` detached,
  publishing a durable capture intent if the spawn fails.
- No shipped hook reaches them. `integrations/claude-code/settings.json` passes
  `--delegate precompact_capture.py` to the adapter, and `integration_adapter._dispatch_cli_event`
  runs a named delegate only when it is *not* this event's capture delegate — `CAPTURE_DELEGATES`
  maps `pre_compact` to exactly that script, so the flag has been inert since the adapter took over
  the capture itself. `session_end_capture.py` is not named anywhere in the shipped configuration
  at all.
- The only other caller was the periodic flush of `user_prompt_capture`, and this morning's fix
  replaced it with `capture_running_session` through the adapter
  (`docs/research/2026-09-17-the-twentieth-prompt-captures-the-session.md`). Checked again today:
  nothing in `scripts/`, `integrations/`, `install.sh`, `install.ps1`, the PowerShell wrapper or
  the OpenCode plugin starts either script. The plugin in the repository
  (`scripts/llm-wiki-memory-opencode.js`, which the installer publishes) calls only
  `integration_adapter.py` and `graph_hint.py`; the July version that called
  `precompact_capture.py` is two rewrites old.
- `flush_memory.py` itself is a different case. It is named in `CLAUDE.md` and `docs/AGENTS.md` as
  an operator-runnable pipeline script, its classification prompt is the one the measurement stand
  scores (`benchmark/run_flush_classification.py`), and its `summarize_with_llm` is what enqueues
  the `flush` task kind that `memory_queue` drains when no provider answered. Retiring it would
  orphan a queue task kind, which is another area's contract.

## Practice on this date

- The owner's standing rule for this audit: a feature nothing runs is either wired in, when a
  contract promises it and the wiring is small, or deleted with its tests and its contract
  sentence — and nothing an outside caller may use is ever deleted. Both scripts fail the first
  test (no contract promises them; the capture they were the front door for is the adapter's own
  path now) and pass the second only in one narrow sense: an installation whose `settings.json`
  still names them directly, from before the adapter existed.
- That case is covered without them: `merge_claude_settings.OUR_SCRIPT_MARKERS` names both scripts,
  so an old hook entry that calls one is recognised as ours and replaced by the adapter entry on
  the next install or upgrade — which is the only way that configuration is ever written.

## The decision

- `scripts/precompact_capture.py` and `scripts/session_end_capture.py` are deleted, with their
  tests.
- The inert `--delegate precompact_capture.py` is removed from the shipped Claude settings.
- `CAPTURE_DELEGATES` keeps naming both scripts. It is now purely a compatibility guard: an older
  `settings.json` that still passes one of those flags to the adapter is ignored, exactly as today,
  instead of failing on a delegate that no longer exists.
- `merge_claude_settings.OUR_SCRIPT_MARKERS` keeps both names, so a direct hook entry from an old
  install is still recognised and replaced rather than left pointing at a script that is gone.
- `flush_memory.py` stays as the operator command it is documented to be; what is retired is the
  path that started it from a hook. `docs/operating-model.md` said the baseline capture path is
  `session_end_capture.py` spawning it, which has not been true since the adapter took the event;
  that sentence is corrected in the same change.

Files: `scripts/precompact_capture.py`, `scripts/session_end_capture.py`,
`scripts/integration_adapter.py`, `integrations/claude-code/settings.json`,
`docs/operating-model.md`, `tests/test_capture_hooks.py`, `tests/test_plugin_helpers.py`,
`tests/test_session_end_skip.py`, `tests/test_integration_injection.py`
