# Runtime Injection and Compile Reliability Design

## Goal

Make the installed LLM Wiki reliably inject context into OpenCode, Codex, and
Claude Code, and compile captured daily logs through the user's authenticated
OpenCode SDK using exactly `openai/gpt-5.6-luna`, without relying on a separate
Codex CLI, another model, or an API key.

## Confirmed Problems

1. The OpenCode plugin registers `session.created` and `session.idle` as direct
   hook keys. Current OpenCode exposes session lifecycle events through the
   universal `event()` hook, so startup context generation and idle processing
   are not reliable.
2. OpenCode exposes working `memory_context` and `memory_recall` tools, but does
   not automatically add the generated knowledge context to a new chat.
3. The Codex wrapper creates `cache/session-context.md`, but Codex does not
   guarantee that an agent reads that file. Current Codex supports native
   lifecycle hooks whose SessionStart output is added directly to developer
   context.
4. The original `maybe_compile.py` PID-file protocol had a publication race and
   required unsafe stale-owner inference after crashes.
5. `llm_client.py` treats any non-empty backend output as success. Authentication
   and compatibility messages such as `Not logged in` can therefore become the
   compile response instead of triggering fallback or deferral.
6. The OpenCode desktop app does not expose the assumed HTTP server at
   `127.0.0.1:4096`. The separate Codex CLI path cannot satisfy the required
   `openai/gpt-5.6-luna` service contract, and the user rejected updating or
   relying on that separate CLI.

## Constraints

- SDK service work must use exactly `openai/gpt-5.6-luna`.
- Do not add active routing or model-override fallback.
- Do not update or depend on the separate Codex CLI.
- Do not require an API key or add paid API usage.
- Keep the installed vault's existing three-zone layout and environment paths.
- Preserve all captured data when a model is unavailable.
- OpenCode plugin/config changes require an OpenCode restart.
- Codex hooks require review/trust in Codex after installation.

## Architecture

### OpenCode Lifecycle Adapter

The plugin will expose one `event({ event })` hook and route
`session.created` and `session.idle` by `event.type`. Existing supported direct
hooks such as `tool.execute.after` and `experimental.session.compacting` remain
direct hooks.

At session creation, the adapter will:

1. record a heartbeat;
2. generate the fallback context file;
3. drain SDK-compatible deferred work;
4. process a pending compile through the authenticated OpenCode SDK;
5. warm search asynchronously.

The plugin will also use `experimental.chat.system.transform` to append the
fresh LLM Wiki context to the chat system/developer context. Injection is
bounded and deduplicated so repeated transforms do not append duplicate memory.
The `memory_context` tool remains available for explicit refreshes.

### SDK Compile Bridge

Compilation will be split into deterministic Python preparation/application
and the model call:

1. Python selects changed daily logs and emits a request containing the prompt,
   system prompt, maximum output tokens, selected paths, and source hashes.
2. The OpenCode plugin submits that request through `client.session.create()` /
   `client.session.prompt()` with
   `model: {providerID: "openai", modelID: "gpt-5.6-luna"}`. It does not use
   active-session routing and has no alternate model-override fallback.
3. Python receives the model response, confirms the daily-file hashes still
   match, parses the strict JSON plan, verifies evidence, applies writes, rebuilds
   indexes, and updates compile state.

If OpenCode is closed, daily logs remain pending. Nightly maintenance still
runs queue recovery, lint, FTS rebuild, and graph rebuild. The next active
OpenCode session performs the deferred `openai/gpt-5.6-luna` compilation.

### Compile Lock Ownership

`run/compile.pid` remains the canonical fixed lock path, but its contents do not
represent a PID or owner. The compile process keeps the file descriptor open and
holds a nonblocking OS lock for its full critical section: `msvcrt.locking()` on
Windows and `fcntl.flock()` on POSIX. Acquisition uses monotonic bounded polling.

The file remains after release. The OS releases ownership when the descriptor is
closed or its process terminates, so no process probes PIDs, infers staleness, or
unlinks another process's lock. SDK and direct-run timeouts are recorded in
`run/state.json` and `logs/compile-sdk-last.log` and return nonzero without a
traceback.

### Durable Compile Journal

Before any note mutation, Python validates every operation and exact evidence
citation, then fsyncs an immutable accepted plan under
`run/compile-journal/<batch-id>.json`. Retries execute that accepted ordered plan
even if a provider returns changed content or targets. Mutable operation and
index status share the journal envelope while an integrity digest protects the
accepted section; completed journals are retained with a bounded policy while
nonterminal audit records are never pruned.

Final source-hash publication creates `compile_index_pending`. Completion is not
reported until index rebuild succeeds, and prepare/direct retries service that
state without another model request. Note, journal, and state replacements flush
the temporary file before replacement and sync the parent directory on POSIX.

### Provider Failure Semantics

Each CLI backend will require a zero exit code and a non-error response. Known
authentication/setup text is failure, not model output. Auto mode may continue
to another eligible provider; a forced provider remains strict. In this local
installation, background compilation is configured for the OpenCode SDK bridge,
so unsupported CLIs are not invoked.

### Native Codex Context Hooks

The installer will merge user-level `~/.codex/hooks.json` entries for:

- `SessionStart`: run `session_start_context.py` and
  `session_start_project_state.py`; both outputs become developer context.
- `PostToolUse`: capture significant tool breadcrumbs.
- `PreCompact`: preserve context before compaction.
- `Stop`: capture the completed turn without launching a lower-model compile.

Commands use absolute installed-vault paths and Windows-specific command
overrides where needed. Existing unrelated user hooks are preserved. The legacy
PowerShell wrapper may remain for command interception, but context correctness
must no longer depend on reading `cache/session-context.md`.

### Claude Code

Keep the existing native SessionStart hooks. Their command output already
matches `hookSpecificOutput.additionalContext` and was verified directly. Tests
will ensure installer changes do not regress this path.

## Error Handling

- Failed SDK model calls leave daily hashes unmarked and retain pending work.
- Changed daily files between prepare and apply invalidate the response and are
  retried from fresh input later.
- Invalid or non-JSON model output records an actionable error without data loss.
- OpenCode event failures are logged through `client.app.log()` with the service
  name `llm-wiki-memory`; silent catch blocks are not used for critical startup
  and compile paths.
- Hook commands always fail open for the host agent: memory failure must not
  prevent OpenCode, Codex, or Claude from starting.

## Testing

Behavioral tests will prove:

1. an OpenCode `event` payload for `session.created` updates heartbeat/context;
2. system transform injects memory once and does not duplicate it;
3. SDK compile preparation, model response, and application complete end to end;
4. changed source hashes reject stale model responses;
5. Windows/POSIX OS-lock contention is bounded and serializes SDK/direct writes;
6. process termination releases ownership without deleting the fixed lock file;
7. non-zero Codex/Claude CLI exits and `Not logged in` output fall through or
   defer instead of being accepted;
8. Codex hook merge preserves unrelated hooks and emits valid SessionStart
   `additionalContext`;
9. OpenCode, Codex, and Claude context paths work from a non-vault project;
10. the full pytest suite, Ruff, structural lint, Windows scheduled tasks, and
    installed-plugin smoke tests pass.

## Documentation and Installation

Update the installer, OpenCode plugin README, Codex integration documentation,
architecture docs, test counts, and changelog together. Re-run the installer,
restart OpenCode, trust the new Codex hooks, and validate fresh sessions in all
three agents.

After local verification, provide a self-contained prompt for an agent working
in the clean public source repository. The prompt must include every issue found
throughout this chat, observed evidence, required tests, current documentation
links, constraints, and completion gates, while instructing that agent to
reproduce each defect independently before changing source.

## Current References

- OpenCode plugin API: https://opencode.ai/docs/plugins/
- Codex hooks: https://developers.openai.com/codex/hooks
- Codex AGENTS.md discovery: https://developers.openai.com/codex/guides/agents-md
- Codex configuration reference:
  https://developers.openai.com/codex/config-reference
