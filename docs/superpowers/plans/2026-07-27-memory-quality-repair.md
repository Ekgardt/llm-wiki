# LLM Wiki Memory Quality Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not commit unless the operator explicitly requests it.

**Goal:** Retain project-scoped user intent and reusable knowledge while stopping telemetry noise, draining deferred work reliably, and rejecting low-quality or duplicate note proposals before write.

**Architecture:** Keep current Markdown and JSON persistence. Add deterministic gates at producer/consumer boundaries, normalize both daily grammars only in memory, and make OpenCode maintenance a bounded re-triggerable state machine. Preserve all historical data and use no additional model calls or dependencies.

**Tech Stack:** Python 3.10+, pytest, JavaScript OpenCode plugin, JSON/Markdown, OpenCode Server/SDK.

**Design:** `docs/superpowers/specs/2026-07-27-memory-quality-repair-design.md`

---

### Task 1: High-Signal Capture And Project Attribution

**Files:**
- Modify: `scripts/llm-wiki-memory-opencode.js`
- Modify: `scripts/post_tool_capture.py`
- Modify: `tests/test_integration_injection.py`
- Modify: `tests/test_capture_hooks.py`

- [ ] **Step 1: Write failing OpenCode capture tests**

Add behavioral tests proving:

```python
def test_opencode_uses_worktree_for_prompt_project_identity(): ...
def test_opencode_suppresses_tool_target_inside_vault_from_parent_workspace(): ...
def test_opencode_captures_file_mutations_but_no_shell_commands(): ...
def test_opencode_idle_transcript_excludes_system_reasoning_and_tool_output(): ...
def test_opencode_idle_prompt_rejects_status_audit_and_code_derived_facts(): ...
```

The Node fixtures must inspect actual `runPython` payloads and classifier prompt
text. They must not assert source-code substrings.

- [ ] **Step 2: Verify the new tests fail for the confirmed behavior**

Run:

```powershell
uv run pytest tests/test_integration_injection.py -q
```

Expected: failures show `directory` instead of `worktree`, Bash capture, static
vault checks, broad transcript parts, and the weak idle prompt.

- [ ] **Step 3: Write failing Python boundary tests**

Replace the test that expects selected Bash commands with:

```python
def test_tool_capture_rejects_all_shell_breadcrumbs(): ...
def test_tool_capture_rejects_target_resolved_inside_vault(): ...
```

Keep existing atomic dedupe, retry, redaction, and concurrency tests.

- [ ] **Step 4: Verify Python tests fail**

Run:

```powershell
uv run pytest tests/test_capture_hooks.py -q
```

- [ ] **Step 5: Implement the minimal capture boundary**

In the plugin:

```javascript
const SIGNIFICANT_TOOLS = new Map([
  ["edit", "Edit"],
  ["write", "Write"],
  ["multi_edit", "MultiEdit"],
  ["notebook_edit", "NotebookEdit"],
  ["apply_patch", "ApplyPatch"],
]);

export const LlmWikiMemoryPlugin = async ({ client, directory, worktree, runtime }) => {
  const projectDirectory = String(worktree || directory || "");
  // Resolve event-local workdir/file target and test containment against
  // LLM_WIKI_ROOT before forwarding a mutation breadcrumb.
};
```

Use `projectDirectory` for prompt context, slug computation, heartbeat, idle,
and precompact provenance. Resolve relative file targets against explicit
`workdir`/`cwd`, then the project directory. Filter transcripts to role
`user|assistant` and part type `text` only. Align the idle classification text
with the durable criteria already present in `flush_memory.py`.

In Python, remove `Bash` from `SIGNIFICANT_TOOLS` and reject a resolved file
target inside `ROOT`, not only a `cwd` inside `ROOT`.

- [ ] **Step 6: Verify focused capture behavior**

Run:

```powershell
uv run pytest tests/test_capture_hooks.py tests/test_integration_injection.py -q
```

---

### Task 2: Re-triggerable Queue And Provenance Round Trip

**Files:**
- Modify: `scripts/llm-wiki-memory-opencode.js`
- Modify: `scripts/flush_memory.py`
- Modify: `scripts/memory_queue.py`
- Modify: `scripts/precompact_capture.py`
- Modify: `scripts/session_end_capture.py`
- Modify: `scripts/codex_memory.py`
- Modify: `tests/test_integration_injection.py`
- Modify: `tests/test_memory_queue.py`
- Modify: `tests/test_flush_classification.py`

- [ ] **Step 1: Write failing maintenance state-machine tests**

Add Node-backed tests:

```python
def test_opencode_queue_cap_schedules_and_drains_remaining_tasks(): ...
def test_opencode_later_session_event_processes_new_queue_work(): ...
def test_opencode_concurrent_maintenance_requests_are_coalesced(): ...
```

The seven-task fixture must assert all seven tasks apply in FIFO order after one
scheduled continuation and compile begins only when the queue is empty.

- [ ] **Step 2: Verify maintenance tests fail**

Run the three tests directly with `pytest path::name -q`. Expected: only five
tasks apply and later triggers do nothing.

- [ ] **Step 3: Write failing provenance tests**

Add tests proving an immediate and a deferred flush retain:

```json
{
  "session_id": "session-1",
  "project_slug": "alpha",
  "project_root": ".../alpha",
  "trigger": "opencode-compacting",
  "occurred_at": "2026-07-27T12:34:56"
}
```

Cover both `_apply_sdk_response()` and manual `drain` through the same renderer.
Also prove a legacy task without these fields still applies.

- [ ] **Step 4: Verify provenance tests fail**

Run:

```powershell
uv run pytest tests/test_memory_queue.py tests/test_flush_classification.py -q
```

- [ ] **Step 5: Implement bounded repeatable maintenance**

Replace `sdkMaintenanceStarted` with:

```javascript
let maintenanceRunning = false;
let maintenanceRequested = false;
let maintenanceContinuationScheduled = false;
```

Implement `requestMaintenance()` so concurrent triggers set `requested`, one
consumer runs at a time, and reaching `MAX_QUEUE_TASKS` schedules one delayed
continuation. Trigger it from non-internal `session.created` and
`experimental.chat.system.transform`. Preserve stop-on-failure and existing
lease/apply contracts.

- [ ] **Step 6: Implement provenance once at the queue boundary**

Add optional `flush_memory.py` arguments:

```text
--project-slug
--project-root
--occurred-at
```

Store them with `session_id`, `trigger`, and `event` in a deferred flush payload.
Create one `memory_queue.py` helper that parses/classifies a response and renders
the daily block; call it from both SDK apply and manual drain. Use occurrence
time when valid and explicit legacy fallbacks otherwise. Forward metadata from
OpenCode, Claude/Codex wrappers, and `codex_memory.py`.

- [ ] **Step 7: Verify queue and provenance behavior**

Run:

```powershell
uv run pytest tests/test_memory_queue.py tests/test_flush_classification.py tests/test_integration_injection.py -q
```

---

### Task 3: Compatible Daily Reader And Bounded Project Context

**Files:**
- Modify: `scripts/session_start_context.py`
- Modify: `integrations/claude-code/settings.json`
- Modify: `integrations/codex/hooks.template.json`
- Modify: `tests/test_context_noise.py`
- Modify: `tests/test_integration_injection.py`
- Modify: `tests/test_merge_claude_settings.py`
- Modify: `tests/test_merge_codex_hooks.py`

- [ ] **Step 1: Write failing daily compatibility/isolation tests**

Add:

```python
def test_daily_excerpt_reads_bullet_only_prompt(): ...
def test_newer_prompt_bullet_is_visible_after_older_heading(): ...
def test_daily_excerpt_skips_tool_bullets(): ...
def test_daily_excerpt_excludes_explicit_other_project(): ...
def test_daily_excerpt_falls_back_to_legacy_unscoped_heading(): ...
def test_latest_useful_daily_skips_empty_newest_file(): ...
```

- [ ] **Step 2: Write failing clipping and duplicate-injection tests**

Add:

```python
def test_bounded_block_never_splits_a_markdown_line(): ...
def test_unused_section_budget_is_given_to_project_state(): ...
def test_combined_session_start_hooks_include_project_state_once(): ...
def test_opencode_context_still_includes_project_state(): ...
```

- [ ] **Step 3: Verify context tests fail**

Run:

```powershell
uv run pytest tests/test_context_noise.py tests/test_integration_injection.py tests/test_merge_claude_settings.py tests/test_merge_codex_hooks.py -q
```

- [ ] **Step 4: Implement an additive daily read model**

Keep `split_session_blocks()` compatibility and add a normalized record reader
for both `## [HH:MM:SS]` blocks and compact prompt/tool bullets. Record source
order, kind, slug, and cleaned lines. `daily_excerpt(path, slug)` must prefer a
matching durable summary, then a matching user prompt, then a legacy unscoped
summary; it must never return tool breadcrumbs or explicit other-project text.
Search a fixed recent-file window when the newest file has no eligible record.

- [ ] **Step 5: Implement line-aware dynamic section budgets**

Change `_bounded_block()` to append only complete lines plus one marker. Start
from the existing reservations, calculate unused characters, and grant them in
order to project state, daily excerpt, then index while preserving
`MAX_CONTEXT_CHARS`.

Add `--omit-project-state`; use it only in Claude/Codex SessionStart commands
that immediately invoke `session_start_project_state.py`. Keep the default for
OpenCode and standalone `memory_context`.

- [ ] **Step 6: Verify all context paths**

Run the focused command from Step 3 and confirm all tests pass.

---

### Task 4: Fail-Closed Durable Knowledge Admission

**Files:**
- Modify: `scripts/compile_memory.py`
- Modify: `tests/test_compile_bounded_batches.py`
- Modify: `tests/test_compile_audit.py`

- [ ] **Step 1: Write failing admission tests**

Add tests:

```python
def test_plain_opencode_audit_status_is_rejected_before_journal(): ...
def test_structured_idle_lesson_is_accepted(): ...
def test_create_existing_slug_is_rejected_not_appended(): ...
def test_update_missing_slug_is_rejected(): ...
def test_create_matching_active_title_is_rejected(): ...
def test_create_matching_active_summary_is_rejected(): ...
def test_two_duplicate_creates_reject_the_whole_plan(): ...
def test_duplicate_outside_prompt_snapshot_is_still_rejected(): ...
def test_provider_audit_count_cannot_bypass_admission(): ...
```

Each rejection must assert no journal, no note mutation, no published daily
hash, and a durable validation error.

- [ ] **Step 2: Verify the tests fail for the expected reasons**

Run each new test directly. Existing successful compile tests must remain
untouched until the new failures are observed.

- [ ] **Step 3: Implement source-quality and action-target checks**

Before `_create_journal()`, reject create evidence from generated idle/deferred
blocks unless the quote belongs to one of the structured durable headings:

```python
DURABLE_SECTION_HEADINGS = {
    "decisions made",
    "lessons / patterns",
    "commands / snippets",
    "gotchas / debugging",
    "open questions",
}
```

Retain legacy unscoped/session-end compatibility. Reject create when its slug
exists and update when it does not.

- [ ] **Step 4: Implement conservative live duplicate checks**

Normalize Unicode case, whitespace, punctuation, and Markdown decoration for
title/summary keys. Scan every active non-archive, non-superseded note at apply
validation time and compare proposed creates against both the live corpus and
earlier creates in the plan. Exact normalized title or one-sentence summary is
grounds for rejection; fuzzy similarity is report-only.

Derive the verified/dedup counts in Python. Provider counters remain metadata
and cannot establish acceptance.

- [ ] **Step 5: Verify compile behavior**

Run:

```powershell
uv run pytest tests/test_compile_bounded_batches.py tests/test_compile_audit.py -q
```

---

### Task 5: Documentation, Full Verification, And Installed Smoke Test

**Files:**
- Modify only if behavior changed: `CHANGELOG.md`, `README.md`, `README.ru.md`, `README.zh-CN.md`, `docs/USER-GUIDE.md`, `AGENTS.md`, `CLAUDE.md`, `tests/README.md`
- Synchronize installed plugin targets after source verification.

- [ ] **Step 1: Update behavior documentation and test counts**

Document mutation-only breadcrumbs, project/worktree attribution, repeatable
queue maintenance, provenance, dual daily parser, and deterministic compile
admission. Keep `AGENTS.md` and `CLAUDE.md` byte-identical and all README
languages synchronized.

- [ ] **Step 2: Run the seven repair gates**

```powershell
git status --short
git ls-files scripts tests integrations docs
uv run ruff check scripts tests
uv run pytest -q
uv run python scripts/lint_memory.py --scope all
git diff --check
```

Also parse `install.ps1` with PowerShell and run `bash -n install.sh` when Git
Bash is available. Inspect imports against tracked files. Do not stage or
commit.

- [ ] **Step 3: Install the verified OpenCode source file**

Copy through the existing installer/synchronization path, not ad-hoc content
editing. Verify both effective plugin targets have the same SHA-256 hash as
`scripts/llm-wiki-memory-opencode.js`.

- [ ] **Step 4: Controlled live verification**

Restart OpenCode only after source tests pass. In an isolated temporary project,
verify:

- a user prompt is attributed to that project;
- read/search/shell activity creates no tool breadcrumb;
- one file edit creates one redacted breadcrumb;
- seven queued fake tasks drain in order across the bounded continuation;
- a bullet-only daily produces useful project-scoped context;
- a plain audit/status compile proposal is rejected before note write;
- Luna metadata is exact and no `memory-*` session remains.

Do not run repair cleanup apply and do not modify existing duplicate notes.

- [ ] **Step 5: Final status inspection**

Run `git status --short` again, list every changed/untracked file, and report
verification evidence plus any live-data caveat. No commit is permitted without
an explicit operator request.
