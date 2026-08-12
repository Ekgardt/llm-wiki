# Eight Code-Quality Findings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden Git execution, bootstrap provenance, feedback identity, structural state parsing, project-context caching, PID filtering, and clipping without committing or touching production.

**Architecture:** Keep trusted process/state primitives in `session_start_project_state.py`, collect bootstrap content and its fingerprint from one deterministic source snapshot in `bootstrap_project.py`, and pass one identity-bound snapshot through `session_start_context.py`. Preserve fail-closed behavior at every malformed, oversized, stale, or rootless boundary.

**Tech Stack:** Python 3.13 standard library, pytest, Ruff, Node.js syntax checking, CommonMark 0.31.2 semantics.

**Research:** Python 3.14.6 recommends fully qualified subprocess executables and warns that `communicate()` buffers data in memory; `shutil.which()` can consult the current directory on Windows. Git documents canonical `--show-toplevel` and stable machine-oriented status formats. CommonMark 0.31.2 defines seven raw HTML block forms.

- https://docs.python.org/3/library/subprocess.html#subprocess.Popen
- https://docs.python.org/3/library/shutil.html#shutil.which
- https://docs.python.org/3/library/stat.html#stat.FILE_ATTRIBUTE_REPARSE_POINT
- https://git-scm.com/docs/git-rev-parse
- https://git-scm.com/docs/git-status
- https://spec.commonmark.org/0.31.2/#html-blocks
- https://docs.python.org/3/library/hashlib.html#hash-algorithms

---

### Task 1: Trusted Bounded Git Execution

**Files:**
- Modify: `scripts/session_start_project_state.py`
- Modify: `scripts/bootstrap_project.py`
- Test: `tests/test_project_state.py`
- Test: `tests/test_bootstrap_project.py`

- [ ] Add behavioral tests proving relative `PATH` entries and a project-local `git` are ignored, the launched executable is an absolute regular non-reparse path resolved before project cwd use, and oversized stdout/stderr fails closed without `capture_output` or `communicate()`.
- [ ] Run the new tests and record the expected security failures.
- [ ] Implement an absolute-only Git resolver and concurrent bounded `Popen` stream drainer; pass one resolved executable through repository identity and all bootstrap Git calls.
- [ ] Run focused Git tests and record GREEN.

### Task 2: Identity-Bound Feedback

**Files:**
- Modify: `scripts/llm-wiki-memory-opencode.js`
- Modify: `scripts/feedback_capture.py`
- Modify: `scripts/build_guardrails.py`
- Test: `tests/test_integration_injection.py`
- Test: `tests/test_feedback_capture.py`
- Test: `tests/test_guardrails.py`

- [ ] Add tests proving OpenCode sends `project_root`, capture rejects mismatched/unconfirmed identity, valid capture persists the canonical root, promotion writes root frontmatter, and rootless legacy candidates remain excluded.
- [ ] Run the new tests and record RED.
- [ ] Thread `project_root` through payload, confirmed capture, candidate schema, promotion, and guardrail matching while retaining legacy candidate readability.
- [ ] Run focused feedback/guardrail tests and record GREEN.

### Task 3: Working-Tree Bootstrap Fingerprint

**Files:**
- Modify: `scripts/bootstrap_project.py`
- Modify: `scripts/session_start_project_state.py`
- Test: `tests/test_bootstrap_project.py`
- Test: `tests/test_project_state.py`

- [ ] Add tests for strict schema/version, dirty tracked README/package input, untracked selected input, relevant non-Git changes, unchanged irrelevant files, no-HEAD repositories, and bounded source inventories/content.
- [ ] Run the new tests and record RED.
- [ ] Collect one bounded deterministic source snapshot containing canonical root, repository kind/head, bounded Git-derived values, and selected file role/path/mode/size/mtime/content SHA-256 descriptors; render from it and publish only if recomputation matches.
- [ ] Require the schema version and source fingerprint during injection and recompute before accepting bootstrap content.
- [ ] Run focused provenance tests and record GREEN.

### Task 4: Shared Structural Open-Thread Reader

**Files:**
- Modify: `scripts/build_advisory.py`
- Modify: `scripts/session_start_project_state.py`
- Test: `tests/test_context_noise.py`
- Test: `tests/test_project_state.py`

- [ ] Add tests proving direct `Path.resolve/open` is not used, file replacement cannot race the identity-bound read, and headings/bullets inside fences, comments, or raw blocks neither open nor close `Open threads`.
- [ ] Run the new tests and record RED.
- [ ] Read through `_read_trusted_state_body` and parse visible H2 boundaries/bullets structurally from the shared same-line-index visibility stream.
- [ ] Run focused advisory tests and record GREEN.

### Task 5: One Project Snapshot Per Context Build

**Files:**
- Modify: `scripts/session_start_context.py`
- Modify: `scripts/session_start_project_state.py`
- Modify: `scripts/build_advisory.py`
- Test: `tests/test_context_noise.py`
- Test: `tests/test_project_state.py`

- [ ] Add subprocess/freshness/state-reader call-count tests for normal builds and zero bootstrap/Git work under `--omit-project-state`.
- [ ] Run the new tests and record RED.
- [ ] Resolve identity without launching bootstrap, load trusted state/threads/bootstrap once, pass cached values to advisory and project renderers, and launch detached bootstrap only from the cached stale result.
- [ ] Avoid constructing the project block entirely when omitted.
- [ ] Run focused context tests and record GREEN.

### Task 6: CommonMark Raw HTML Visibility

**Files:**
- Modify: `scripts/session_start_project_state.py`
- Test: `tests/test_project_state.py`

- [ ] Parameterize CommonMark HTML block types 1-7 with hidden ownership, handoff, and section-heading decoys plus visible siblings.
- [ ] Run the new tests and record RED.
- [ ] Extend `_state_visible_lines` with closer-terminated and blank-terminated raw block state while preserving line-count/index alignment.
- [ ] Run focused visibility tests and record GREEN.

### Task 7: Safe PID Noise Filtering

**Files:**
- Modify: `scripts/session_start_project_state.py`
- Test: `tests/test_project_state.py`

- [ ] Add tests for overlong digits, zero, values above `2_147_483_647`, and injected `ValueError`, `OverflowError`, and `OSError` from process probing.
- [ ] Run the new tests and record RED.
- [ ] Validate digit count/range before conversion and classify malformed process-status metadata as noise without suppressing durable PID discussion.
- [ ] Run focused PID tests and record GREEN.

### Task 8: Identity-First Explicit Clipping

**Files:**
- Modify: `scripts/session_start_project_state.py`
- Modify: `scripts/session_start_context.py`
- Test: `tests/test_project_state.py`
- Test: `tests/test_context_noise.py`

- [ ] Add long Unicode handoff/bootstrap tests for standalone context, combined project block, and complete `build_context`; assert canonical identity precedes variable content, remains complete, output is valid UTF-8, and every omission has an explicit line/section marker.
- [ ] Run the new tests and record RED.
- [ ] Reorder identity before handoff/bootstrap and replace silent slicing with Unicode-code-point-safe, explicitly marked line/section clipping that reserves mandatory identity first.
- [ ] Run focused clipping tests and record GREEN.

### Task 9: Integrated Verification and Count Sync

**Files:**
- Modify only operational test-count documentation whose live values changed.

- [ ] Run all changed-area tests together.
- [ ] Run `uv run --no-sync pytest --collect-only -q` and synchronize every operational count.
- [ ] Run `uv run --no-sync pytest -q`.
- [ ] Run `uv run --no-sync ruff check scripts/ tests/`.
- [ ] Run `node --check scripts/llm-wiki-memory-opencode.js`.
- [ ] Run import/count guards, `git diff --check`, `AGENTS.md`/`CLAUDE.md` parity, dependency searches, and `git status --short`.
- [ ] Do not stage, commit, deploy, install, start servers, or access production.
