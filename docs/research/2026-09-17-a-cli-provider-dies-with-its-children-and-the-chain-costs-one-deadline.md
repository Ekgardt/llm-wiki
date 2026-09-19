# A CLI provider dies with its children, and the chain costs one deadline

Dated 2026-09-17. The leftovers of finding M-A8 of the third audit — the three items the
first round named and left — plus the same task framing for the other two CLI-shaped
providers. The research before the fix.

Files: `scripts/llm_client.py`, `scripts/compile_memory.py`,
`scripts/contradiction_pipeline.py`, `scripts/sync_memory.py`,
`tests/test_a_cli_provider_dies_with_its_children.py`,
`tests/test_the_task_is_named_to_the_model.py`.

## What was found

- **The bound is not a bound for a wrapper.** `_call_claude` and `_codex_last_message` use
  `subprocess.run(..., timeout=...)`, which kills the direct child only. On Windows both
  CLIs are npm shims (`claude.cmd`, `codex.cmd`); a shim's grandchild keeps the inherited
  pipes, and `run()`'s post-kill `communicate()` then waits for them with no bound at all.
  The product already owns the remedy — `sync_memory._run_process_tree`, written for the
  same defect in maintenance steps (`docs/research/2026-09-10-a-step-that-times-out-takes-its-children-with-it.md`):
  POSIX `start_new_session` plus `killpg`, Windows `CREATE_NEW_PROCESS_GROUP` plus
  `taskkill /T /F`, a bounded drain, and a `cleanup_error` when the tree could not be
  proved gone. `llm_client` did not use it.
- **`codex.ps1` cannot be executed.** `_windows_codex_candidate` returns the first of
  `codex.cmd`, `codex.ps1`, `codex.exe` that exists under `%APPDATA%\npm`. A PowerShell
  script is not an executable image: `CreateProcess` refuses it, so an install that has
  `codex.ps1` but not `codex.cmd` reports `provider_error` for ever.
- **The chain pays every deadline.** Auto mode tries opencode, codex, claude, openai,
  ollama in turn and falls through on any failure, a timeout included. Every step budget
  in `scheduled_nightly` is sized for one deadline (`STEP_START_MARGIN_SECONDS = 120`
  against a 90 s call), so two slow CLIs already overrun the margin and the step is killed
  with its paid work unsaved. Three chains have this shape: `call_llm_result`,
  `_CompileAttempt`, and `ContradictionPipeline._provider_stage`.
- **Only claude frames the task.** The Claude provider wraps the prompt in `<task>` and
  names the frame in the system text, after the measurement of this morning
  (`docs/research/2026-09-17-the-task-is-named-to-the-model.md`). Codex and OpenCode carry
  the same exposure by construction: `codex exec` reads instruction files from
  `$CODEX_HOME` before the prompt, and an OpenCode server prepends whatever its own
  configuration and plugins put in the session — this vault ships such a plugin itself.

## Practice on this date

- "If the timeout expires, the child process will be killed and waited for. The
  TimeoutExpired exception will be re-raised after the child process has terminated."
  ([subprocess, Python 3 documentation](https://docs.python.org/3/library/subprocess.html)).
  The killing is of the child, not of its descendants; the standard library says nothing
  about a tree, which is why the product has its own runner.
- "XML tags help Claude parse complex prompts unambiguously … Wrapping each type of
  content in its own tag … reduces misinterpretation."
  ([Prompting best practices, Structure prompts with XML tags](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)).
  The frame is provider-independent text, so applying it to the other CLI-shaped providers
  needs no second measurement to be safe; what it does need is to cost little, and it
  costs about 50 input tokens a call.

## The decision

- Both CLI backends run through one helper that uses the product's process-tree runner, so
  a timeout ends the wrapper and everything it started, and a tree that could not be proved
  gone is named (`provider_timeout`, with the cleanup error printed). The runner is
  imported inside the function: `sync_memory` imports `doctor` at module level, and
  `llm_client` is imported by hooks that must stay cheap. `sync_memory._run_process_tree`
  gains an optional `input=`, the smallest change that lets a prompt keep travelling
  through a pipe instead of a temporary file on disk.
- `codex.ps1` is no longer a candidate. `.cmd` and `.exe` are the two spellings a
  `CreateProcess` can start; npm writes the `.cmd` shim beside the `.ps1` one.
- A `provider_timeout` ends the chain in all three chains, through one shared rule named
  once (`chain_stops_after`). A deadline is the budget of the whole call: the next provider
  would spend a second one that the step was never given. Every other failure still falls
  through, and the fast common case — a provider that is not installed or not logged in —
  is a probe or an immediate exit, not a timeout.
- Codex and OpenCode frame the task exactly as claude does: the prompt inside
  `<task>…</task>`, and `TASK_FRAME` appended to the system text they carry. No path,
  environment variable or contract changes.
