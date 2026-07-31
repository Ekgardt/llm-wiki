# Runtime Injection and Compile Reliability Implementation Plan

> **Status: superseded on 2026-07-25 by
> `2026-07-25-installed-reliability-repair.md`.** This file is a historical
> implementation plan, not an active completion checklist. Its unchecked steps
> must not be interpreted as current release qualification, and they are left
> unchanged because their original execution evidence was not recorded here.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make OpenCode and Codex inject LLM Wiki context automatically and make pending daily logs compile through the OpenCode SDK using exactly `openai/gpt-5.6-luna`, without a separate CLI, alternate model, or API key.

**Architecture:** Python owns deterministic request preparation, evidence validation, state updates, and file writes. The OpenCode plugin owns authenticated model execution and lifecycle routing. Codex and Claude use native SessionStart hooks for direct developer-context injection. Compile serialization uses a fixed OS-backed lock file held by the compiler process.

**Tech Stack:** Python 3.10+, pytest, Node.js/Bun-compatible JavaScript, OpenCode plugin SDK, Codex lifecycle hooks JSON, PowerShell/Bash installers.

---

## File Map

- Modify `scripts/llm-wiki-memory-opencode.js`: official OpenCode event routing, system-context injection, SDK compile execution, structured logging.
- Modify `scripts/compile_memory.py`: prepare/apply bridge with source-hash validation.
- Modify `scripts/maybe_compile.py`: OpenCode-deferred mode and OS-lock probing.
- Modify `scripts/memory_state.py`: cross-platform compile lock primitive.
- Modify `scripts/llm_client.py`: reject failed CLI calls and setup/authentication text.
- Create `scripts/merge_codex_hooks.py`: preserve user hooks and install LLM Wiki native hooks.
- Create `integrations/codex/hooks.template.json`: canonical Codex hook definitions.
- Modify `install.ps1` and `install.sh`: install OpenCode and Codex integration, set deferred provider mode.
- Modify `scripts/codex-memory-wrapper.ps1`: remove context-file correctness claims and lower-model compile triggering.
- Modify tests under `tests/`: behavioral regression coverage for each defect.
- Modify architecture/user docs and test-count references together.

### Task 1: OS-Backed Compile Lock

**Files:**
- Modify: `tests/test_maybe_compile.py`
- Modify: `scripts/memory_state.py`
- Modify: `scripts/maybe_compile.py`
- Modify: `scripts/compile_memory.py`

- [ ] **Step 1: Write failing lock-behavior tests**

Cover lock contention, bounded timeout, fixed-file persistence, release after
process termination, and trigger status without interpreting file contents.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_maybe_compile.py -q`

Expected: failure because the existing lock is based on PID contents and stale-file deletion.

- [ ] **Step 3: Implement OS-backed ownership**

Keep `run/compile.pid` open for the full compile. Use `msvcrt.locking()` on
Windows and `fcntl.flock()` on POSIX with monotonic bounded polling. Never parse
or unlink the fixed lock file. Persist timeout failures at SDK and direct CLI
boundaries.

- [ ] **Step 4: Verify GREEN and platform behavior**

Run: `uv run pytest tests/test_maybe_compile.py -q`

Expected: all tests pass on Windows and POSIX; forced process termination frees
the lock through OS descriptor cleanup.

### Task 2: Reject Failed LLM Backends

**Files:**
- Create: `tests/test_llm_client.py`
- Modify: `scripts/llm_client.py:52-107,254-352`

- [ ] **Step 1: Write failing backend-result tests**

Cover Codex/Claude subprocess return code, empty output, `Not logged in`, model
compatibility errors, and auto fallback to the next provider. Keep forced
provider behavior strict.

```python
def test_auto_mode_rejects_not_logged_in_and_falls_through(monkeypatch):
    monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)
    monkeypatch.setattr(llm_client, "_candidate_order", lambda _: ["claude", "opencode"])
    monkeypatch.setitem(llm_client._PROBES, "claude", lambda: True)
    monkeypatch.setitem(llm_client._PROBES, "opencode", lambda: True)
    monkeypatch.setitem(llm_client._BACKENDS, "claude", lambda *_: "Not logged in · Please run /login")
    monkeypatch.setitem(llm_client._BACKENDS, "opencode", lambda *_: "valid response")
    assert llm_client.call_llm("prompt") == "valid response"
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_llm_client.py -q`

Expected: authentication text is incorrectly accepted.

- [ ] **Step 3: Implement failure classification**

Return empty output from CLI backends on non-zero exit. Add a narrow
`_is_backend_error_text()` predicate for known authentication/setup/model
compatibility responses and make `call_llm` continue in auto mode.

- [ ] **Step 4: Verify GREEN**

Run: `uv run pytest tests/test_llm_client.py -q`

Expected: all provider fallback tests pass.

### Task 3: Deterministic SDK Compile Bridge

**Files:**
- Modify: `tests/test_compile_audit.py`
- Modify: `tests/test_compile_failure.py`
- Modify: `scripts/compile_memory.py:188-329,896-1069`

- [ ] **Step 1: Write failing prepare/apply tests**

Test a prepared request containing prompt, system prompt, max tokens, selected
daily paths, and SHA-256 hashes. Test applying a fake strict-JSON response. Test
that changing a daily after preparation rejects the response without marking its
hash compiled.

```python
def test_apply_prepared_compile_rejects_changed_daily(tmp_path, monkeypatch):
    request = compile_memory.prepare_compile_request([daily])
    daily.write_text("changed after request", encoding="utf-8")
    result = compile_memory.apply_compile_response(request, EMPTY_PLAN_JSON, False)
    assert result.status == "stale-input"
    assert daily.name not in load_state().get("compiled_daily_hashes", {})
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_compile_audit.py tests/test_compile_failure.py -q`

Expected: prepare/apply APIs do not exist.

- [ ] **Step 3: Extract request preparation and response application**

Move prompt assembly into `prepare_compile_request(daily_paths)`. Move JSON
parsing and `_execute_plan` invocation into
`apply_compile_response(request, raw_response, dry_run)`. Add CLI modes
`--prepare-sdk-request` and `--apply-sdk-response`; stdin/stdout use JSON and
never shell interpolation.

- [ ] **Step 4: Preserve existing synchronous compile behavior**

Make `run_compile` call prepare, `call_llm`, then apply. Existing fake-provider
tests must remain green.

- [ ] **Step 5: Verify GREEN**

Run: `uv run pytest tests/test_compile_audit.py tests/test_compile_failure.py tests/test_audit_fixes.py -q`

Expected: prepare/apply and legacy synchronous paths pass.

### Task 4: OpenCode Lifecycle and Automatic Injection

**Files:**
- Modify: `tests/test_integration_injection.py`
- Modify: `scripts/llm-wiki-memory-opencode.js`

- [ ] **Step 1: Add a behavioral Node harness**

Instantiate the plugin with a fake client and fake vault interpreter. Send
`event({event:{type:"session.created",properties:{info:{id:"s1"}}}})` and
assert heartbeat/context/SDK compile calls happen. Invoke
`experimental.chat.system.transform` twice and assert one bounded memory block.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_integration_injection.py -q`

Expected: direct `session.created` key is not reachable through `event`, and no
system transform exists.

- [ ] **Step 3: Implement official event routing**

Replace direct session lifecycle keys with `event: async ({event}) => { ... }`.
Route `session.created` and `session.idle` using `event.type`. Keep supported
direct tool and compaction hooks unchanged.

- [ ] **Step 4: Implement bounded system-context injection**

Generate context with Python, append a marker-delimited block in
`experimental.chat.system.transform`, and skip insertion when the marker is
already present. Fail open and log through `client.app.log()`.

- [ ] **Step 5: Implement OpenCode SDK compilation**

Run Python `--prepare-sdk-request`; if work exists, create an ephemeral OpenCode
session, send system and user prompts through `client.session.prompt`, collect
text, delete the session, and pass the response to Python
`--apply-sdk-response`. Never specify a lower model override.

- [ ] **Step 6: Verify GREEN**

Run: `uv run pytest tests/test_integration_injection.py -q`

Expected: event routing, single injection, tools, and SDK compile bridge pass.

### Task 5: Native Codex Hooks

**Files:**
- Create: `integrations/codex/hooks.template.json`
- Create: `scripts/merge_codex_hooks.py`
- Create: `tests/test_merge_codex_hooks.py`
- Modify: `install.ps1`
- Modify: `install.sh`
- Modify: `scripts/codex-memory-wrapper.ps1`

- [ ] **Step 1: Write failing merge tests**

Mirror the Claude merge tests: preserve unrelated user hooks, replace only
commands owned by LLM Wiki, write a timestamped backup, and generate valid
Windows/Unix absolute commands. Assert SessionStart output scripts are the two
verified context injectors.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_merge_codex_hooks.py -q`

Expected: merger and template do not exist.

- [ ] **Step 3: Implement template and safe merge**

Define SessionStart, PostToolUse, PreCompact, and Stop command hooks according
to the official Codex hooks schema. Use `commandWindows`, bounded timeouts, and
absolute vault paths. Preserve non-LLM-Wiki hooks and create a backup before
writing `~/.codex/hooks.json`.

- [ ] **Step 4: Wire installers and wrapper**

Run the merger when Codex config exists. Remove the wrapper's claim that Codex
reads `cache/session-context.md`; do not trigger external lower-model compile
from the wrapper when OpenCode SDK mode is selected.

- [ ] **Step 5: Verify GREEN**

Run: `uv run pytest tests/test_merge_codex_hooks.py tests/test_integration_injection.py -q`

Expected: merge and cross-agent injection tests pass.

### Task 6: Deferred OpenCode-Only Maintenance Mode

**Files:**
- Modify: `tests/test_maybe_compile.py`
- Modify: `scripts/maybe_compile.py`
- Modify: `scripts/scheduled_nightly.py`
- Modify: `install.ps1`
- Modify: `install.sh`

- [ ] **Step 1: Write failing deferred-mode tests**

Set `MEMORY_LLM_PROVIDER=opencode-sdk` and assert `maybe_compile` reports pending
work without spawning a child. Assert nightly continues lint/index/graph and
does not mark the run failed merely because compilation awaits OpenCode.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_maybe_compile.py -q`

Expected: current code spawns external compilation.

- [ ] **Step 3: Implement deferred mode**

Return `skipped: pending compile deferred to OpenCode SDK` from
`spawn_compile_if_idle` in this mode. Install the user-scope provider variable
without changing vault/state paths. Keep direct `--force` diagnostic behavior
explicit and documented.

- [ ] **Step 4: Verify GREEN**

Run: `uv run pytest tests/test_maybe_compile.py -q`

Expected: no external LLM process is launched in OpenCode-only mode.

### Task 7: Documentation, Installation, and End-to-End Verification

**Files:**
- Modify: `README.md`, `README.ru.md`, `README.zh-CN.md`
- Modify: `CHANGELOG.md`, `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`
- Modify: `docs/STRUCTURE.md`, `docs/USER-GUIDE.md`, `tests/README.md`
- Modify: `integrations/README.md` and OpenCode plugin README source if present

- [ ] **Step 1: Update behavior and test counts**

Document OpenCode SDK-only GPT-5.6 compilation, deferred nightly behavior,
native Codex hook trust, provider failures, and lock ownership. Keep all three
README languages synchronized.

- [ ] **Step 2: Run focused documentation tests**

Run: `uv run pytest tests/test_readme_i18n.py tests/test_quality_guards.py -q`

Expected: all documentation invariants pass.

- [ ] **Step 3: Run all seven fix-discipline gates**

Run:

```powershell
git status --short
git diff --check
uv run ruff check scripts\ tests\
uv run pytest -q
uv run python scripts\lint_memory.py
```

Expected: Ruff clean, full suite green, lint 0 findings, no untracked imported
Python modules. Review cross-platform path and lock branches explicitly.

- [ ] **Step 4: Reinstall and restart integrations**

Run `pwsh -NoProfile -File .\install.ps1`, quit/restart OpenCode, and review/trust
the new Codex hooks with `/hooks` in Codex. Do not update the separate Codex CLI.

- [ ] **Step 5: Behavioral smoke tests**

Verify from a non-vault project:

- OpenCode new session receives the marker-delimited memory context without a
  manual tool call.
- `memory_context` and `memory_recall` still work.
- A pending daily compiles through the OpenCode SDK using its active GPT-5.6
  route and clears pending state without a stale lock.
- Codex SessionStart hook returns both global and project context.
- Claude SessionStart hooks still return both contexts.
- Nightly and weekly scheduled tasks return result code 0.

- [ ] **Step 6: Produce the source-project agent prompt**

Write a self-contained Russian prompt that lists every defect and warning found
in this chat, references the installed evidence paths and current docs, requires
independent reproduction and TDD, prohibits lower models and separate CLI
dependency, and specifies all verification gates. Include the prompt in the
final response, not as an untracked source file.

- [ ] **Step 7: Do not commit without explicit user instruction**

Report the final `git status --short` and separate pre-existing changes from
changes made by this plan. Do not stage or commit unless the user explicitly
requests it.
