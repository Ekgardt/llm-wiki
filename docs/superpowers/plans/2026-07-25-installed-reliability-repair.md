# Installed LLM Wiki Reliability Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair every confirmed installed-runtime finding without replacing user knowledge or migrating to public v4.

**Architecture:** Keep the installed v3 plugin/SDK bridge, add bounded and explicit service-work contracts, and route cleanup through one dry-run/apply migration. Preserve existing atomic state and daily writers while adding behavioral tests at each production boundary.

**Tech Stack:** Python 3.13, pytest, JavaScript OpenCode plugin, OpenCode Server/SDK, PowerShell 7, Windows Task Scheduler.

## Status And Evidence

Repository implementation and all production gates are complete. The cleanup
ran through separate audit, backup-only, explicit-manifest apply, and verify
stages. Both effective OpenCode plugin targets were synchronized, OpenCode
Desktop was fully restarted, and the live capture/maintenance pipeline was
verified against the installed vault.

Local Windows evidence: 469 tests collected, 466 passed, 3 skipped. Collection
is platform-stable; skip count varies with optional Bash, PowerShell, and
symlink availability. Ruff, documentation parity, PowerShell parsing, Git Bash
syntax, memory lint, and diff checks were also run locally. The final memory
lint has one expected `stale_compiled` advisory for the actively written
current-day log; completed backlog generations, queue tasks, and service
sessions were empty at the verification boundary.

---

### Task 1: Capture Hygiene And User Prompts

**Files:**
- Modify: `scripts/llm-wiki-memory-opencode.js`
- Modify: `scripts/post_tool_capture.py`
- Modify: `scripts/user_prompt_capture.py`
- Test: `tests/test_integration_injection.py`
- Test: `tests/test_capture_hooks.py`

- [x] Add failing Node tests proving OpenCode forwards user message text, skips empty tool targets, and coalesces repeated `(slug, tool, target)` events.
- [x] Run `uv run pytest tests/test_integration_injection.py tests/test_capture_hooks.py -q`; verify the new tests fail for missing prompt forwarding and blank breadcrumbs.
- [x] Add an OpenCode message event handler that forwards only user-role text, normalize `input.args`/`input.input`, and reuse the Python target filter/dedupe path.
- [x] Run the focused tests and verify no blank or duplicate breadcrumbs are written.

### Task 2: Idle, Feedback, And PreCompact Semantics

**Files:**
- Modify: `scripts/llm-wiki-memory-opencode.js`
- Modify: `scripts/precompact_capture.py`
- Modify: `scripts/flush_memory.py`
- Modify: `scripts/feedback_capture.py`
- Test: `tests/test_flush_classification.py`
- Test: `tests/test_feedback_capture.py`
- Test: `tests/test_integration_injection.py`

- [x] Add failing tests for tier-only `FLUSH_MAJOR/MINOR`, ordinary sentences containing `not/must`, and OpenCode precompact carrying transcript plus `trigger=opencode-compacting`.
- [x] Reject non-OK classifications with an empty distilled body.
- [x] Capture feedback only from direct user text and replace broad word matches with correction-shaped phrases.
- [x] Collect the bounded session transcript before compaction and pass both transcript and trigger to Python.
- [x] Run the focused tests and verify all three regressions are closed.

### Task 3: Project-Aware Context Budget

**Files:**
- Modify: `scripts/session_start_context.py`
- Modify: `scripts/session_start_project_state.py`
- Modify: `scripts/llm-wiki-memory-opencode.js`
- Test: `tests/test_context_noise.py`
- Test: `tests/test_integration_injection.py`

- [x] Add a failing test with an oversized index that requires guardrails, health, project state, latest daily, and recent log headings to survive.
- [x] Replace whole-string truncation with per-section budgets and resolve project state from the active directory rather than the latest global heartbeat.
- [x] Run focused context tests and assert total output remains bounded.

### Task 4: Exception-Safe Ephemeral Sessions

**Files:**
- Modify: `scripts/llm-wiki-memory-opencode.js`
- Test: `tests/test_integration_injection.py`

- [x] Add failing Node tests for classifier prompt failure, compile prompt failure, delete failure, and cross-restart title filtering.
- [x] Give each service operation one session owner and implement cleanup as `abort`, `delete`, and `internalSessionIds.delete` in `finally`.
- [x] Persist cleanup failures through `client.app.log` without masking the original provider error.
- [x] Run focused tests and verify no service session remains in any failure path.

### Task 5: SDK Queue Bridge

**Files:**
- Modify: `scripts/memory_queue.py`
- Modify: `scripts/llm-wiki-memory-opencode.js`
- Test: `tests/test_memory_queue.py`
- Test: `tests/test_integration_injection.py`

- [x] Add failing tests for `--prepare-sdk-task` and `--apply-sdk-result`, including stale task hashes and failed provider responses.
- [x] Implement a one-task-at-a-time JSON bridge that leases a queue item, validates its digest on apply, and acknowledges only a successful result.
- [x] Drain a bounded number of tasks during one plugin maintenance run using Luna.
- [x] Run focused tests and verify failed work remains queued with durable attempt metadata.

### Task 6: Bounded Compile Batches

**Files:**
- Modify: `scripts/compile_memory.py`
- Modify: `scripts/maybe_compile.py`
- Test: `tests/test_compile_audit.py`
- Test: `tests/test_maybe_compile.py`
- Add: `tests/test_compile_bounded_batches.py`

- [x] Add failing tests using a 1.5-million-character noisy backlog and one oversized meaningful block.
- [x] Parse daily files into timestamp blocks, discard empty tool/idle noise, and pack meaningful blocks under an explicit prompt-character budget.
- [x] Keep per-daily batch completion state; publish the compiled hash only after all batches succeed.
- [x] Persist prepare/provider/apply failures in `state.json` and `logs/compile-sdk-last.log`.
- [x] Run focused compile tests and verify every generated prompt stays below the configured budget.

### Task 7: Maintenance Truthfulness

**Files:**
- Modify: `scripts/scheduled_nightly.py`
- Modify: `scripts/scheduled_weekly.py`
- Test: `tests/test_scheduled_nightly.py`
- Add: `tests/test_scheduled_weekly.py`

- [x] Add failing tests where detached compile fails quickly, queue remains pending, and weekly mutates pages after its first rebuild.
- [x] Make nightly inspect final compile state and queue status before returning success.
- [x] Hold one weekly lease across the complete sequence and rebuild Markdown index, FTS, and graph after the final mutation.
- [x] Run focused scheduler tests and verify failures produce nonzero task results.

### Task 8: Installer And Windows Lock Hardening

**Files:**
- Modify: `install.ps1`
- Modify: `install.sh`
- Modify: `scripts/daily_log_append.py`
- Modify: `tests/test_quality_guards.py`
- Modify: `tests/test_security_invariants.py`

- [x] Add failing tests for XDG destination selection, installer pytest failure, transient Windows `PermissionError`, and exact expected writer count.
- [x] Install to the effective XDG OpenCode directory plus the Windows compatibility fallback without duplicate paths.
- [x] Stop both installers immediately when dependency sync or pytest fails.
- [x] Treat transient Windows create/delete sharing violations as contention and make the behavioral test assert all 50 writes.
- [x] Run focused tests, PowerShell parser, and `bash -n install.sh`.

### Task 9: Reversible Production Cleanup

**Files:**
- Add: `scripts/repair_installed_memory.py`
- Add: `tests/test_repair_installed_memory.py`

- [x] Add failing tests for dry-run immutability, backup manifest integrity, selective breadcrumb removal, false-feedback quarantine, duplicate-note classification, and ordinary-session preservation.
- [x] Implement `audit`, `apply`, and `verify` modes. `apply` must require a successful backup manifest and write an exact JSON report.
- [x] Implement staged backups under `run/backups/<timestamp>/`, removal of only verified noise, false-feedback quarantine, and report-only duplicate classification. Mutating apply requires the explicit reviewed manifest created by a prior backup-only stage.
- [x] Run focused tests, then run production `audit` mode and review its report before any backup or `apply`.

### Task 10: Documentation And Full Verification

**Files:**
- Modify: `README.md`
- Modify: `README.ru.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/USER-GUIDE.md`
- Modify: `CHANGELOG.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`

- [x] Update only verified behavior, test counts, XDG ownership, model contract, queue semantics, and troubleshooting.
- [x] Run `uv run pytest -q`, `uv run ruff check scripts tests`, docs parity tests, PowerShell parser, Git Bash syntax, memory lint, and `git diff --check`.
- [x] Run the cleanup migration backup/apply/verify sequence.
- [x] Install the plugin into every effective config target and restart OpenCode.
- [x] Run live prompt/tool/idle/precompact/compile/queue smoke tests and verify Luna metadata, zero orphan service sessions, bounded compile prompts, current durable notes, complete context sections, and canonical retrieval.
- [x] Inspect `git status --short`; do not stage or commit without explicit permission.
