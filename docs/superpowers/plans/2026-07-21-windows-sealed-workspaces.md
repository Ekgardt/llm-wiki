# Windows Sealed Workspaces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make analyzer workspace sealing fully usable on qualified Windows hosts while preserving POSIX safety and bounding verification depth and resource use.

**Architecture:** Add one focused Windows filesystem-capability module that wraps only the native handle operations needed by sealed workspaces. Keep snapshot validation and tree traversal in `code_workspace.py`; dispatch to platform-specific create/verify implementations that both enforce capture-compatible depth before opening child directories. Capability probing fails closed and reports the first unavailable safety primitive.

**Tech Stack:** Python 3.10, `ctypes`, Win32 `Kernel32`, NT native file APIs from `ntdll`, pytest, Ruff.

---

### Task 1: Windows Capability Boundary

**Files:**
- Create: `scripts/windows_workspace.py`
- Test: `tests/test_code_workspace.py`

- [ ] Add tests that a non-Windows import is safe, capability probing returns a precise unavailable reason, and missing required exports fail closed.
- [ ] Run `uv run pytest tests/test_code_workspace.py -k "workspace and (capability or supported)" -q` and confirm the new tests fail.
- [ ] Define the Windows structures, constants, typed function bindings, `capability() -> tuple[bool, str | None]`, and a guarded API singleton. Require `NtCreateFile`, `NtOpenFile`, `RtlNtStatusToDosError`, `CloseHandle`, `GetFileInformationByHandleEx`, `ReadFile`, `WriteFile`, `FlushFileBuffers`, and `SetFileInformationByHandle`.
- [ ] Run the targeted tests and confirm they pass.

### Task 2: Relative Handle Operations

**Files:**
- Modify: `scripts/windows_workspace.py`
- Test: `tests/test_code_workspace.py`

- [ ] Add real-Windows tests for exclusive root/directory/file creation, rejection of preexisting components, relative no-follow opens, reparse rejection, stable `FILE_ID_INFO`, handle-based enumeration, bounded reads/writes, read-only attributes, and complete handle closure.
- [ ] Run the new tests and confirm failure before implementation.
- [ ] Implement absolute root-parent opening once with `CreateFileW(..., FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS)`. Implement every descendant create/open with a one-component `UNICODE_STRING`, a held `RootDirectory` handle, `OBJ_CASE_INSENSITIVE`, `FILE_OPEN_REPARSE_POINT`, `FILE_DIRECTORY_FILE` or `FILE_NON_DIRECTORY_FILE`, and `FILE_CREATE`/`FILE_OPEN` as appropriate.
- [ ] Query `FileAttributeTagInfo` after each open and reject reparse points. Query `FileIdInfo` and expose `(volume_serial, 16-byte-id, kind)` identities. Enumerate with handle-bound `GetFileInformationByHandleEx(FileIdExtdDirectoryInfo)` and reject malformed records and normalization/case-fold collisions.
- [ ] Implement synchronous bounded `WriteFile` and `ReadFile`, `FlushFileBuffers` for files, `FileBasicInfo` read-only application, and best-effort directory flush returning whether the primitive succeeded. Never claim a control that failed.
- [ ] Run the targeted native tests and confirm they pass without elevated privileges.

### Task 3: Windows Sealing

**Files:**
- Modify: `scripts/code_workspace.py`
- Test: `tests/test_code_workspace.py`

- [ ] Add Windows tests that `workspace_sealing_supported()` is true on the qualified host, a snapshot seals and verifies, destination preexistence fails, an internal precreated directory fails, owner-only/read-only flags reflect applied controls, and all created files are read-only.
- [ ] Run those tests and confirm failure.
- [ ] Dispatch `seal_workspace()` by platform. Create the destination exclusively relative to a held parent. Reuse bound directory identities while walking each source path, create files exclusively through held parent handles, reject reparse points, write exactly the captured bytes, flush file buffers, apply read-only attributes, and retain only O(depth) handles.
- [ ] Re-open the destination relative to its held parent and compare `FILE_ID_INFO` before returning. Set `owner_only` only if the Windows ACL creation/control is actually applied; otherwise report `False`. Set `read_only_requested` only after every file received and retained the read-only attribute. Do not claim directory durability when directory flush is unavailable.
- [ ] Run the targeted sealing tests and confirm they pass.

### Task 4: Windows Verification

**Files:**
- Modify: `scripts/code_workspace.py`
- Modify: `scripts/windows_workspace.py`
- Test: `tests/test_code_workspace.py`

- [ ] Add real-Windows tests for changed, missing, and extra files; reparse substitution; held-parent substitution resistance; component identity changes; and stable handle closure.
- [ ] Run those tests and confirm failure.
- [ ] Implement iterative handle-relative enumeration. Reject reparse points and devices, enforce entry and directory limits, open each expected file through its held parent, compare enumerated and opened `FILE_ID_INFO`, hash bytes from the held handle with the capture chunk bound, recheck identity/size/attributes, and compare exact sorted membership plus source manifest.
- [ ] Re-open the workspace root relative to the same held parent and compare identity before returning. Convert native failures to `WorkspaceChanged` while preserving fail-closed behavior.
- [ ] Run the targeted verification tests and confirm they pass.

### Task 5: Shared Depth Guard and Resource Bound

**Files:**
- Modify: `scripts/code_workspace.py`
- Test: `tests/test_code_workspace.py`

- [ ] Add POSIX and Windows tests that inject an over-depth directory after sealing and instrument descriptor/handle opens and closes. Assert rejection occurs before opening the over-depth child, no resource leaks remain, and peak resources do not exceed `max_depth + 12`.
- [ ] Run those tests and confirm the existing POSIX verifier fails the pre-open assertion.
- [ ] Track the synthetic workspace root at depth `-1`, making direct children depth `0`, matching capture traversal. Before every child-directory open in both verifiers, reject `child_depth > limits.max_depth`. Preserve existing entry/directory-limit accounting.
- [ ] Run the depth tests and all workspace tests.

### Task 6: Verification and Commit

**Files:**
- Modify only files listed above.

- [ ] Run Windows workspace tests: `uv run pytest tests/test_code_workspace.py -k "workspace or seal" -q`.
- [ ] Run Task 5: `uv run pytest tests/test_code_workspace.py tests/test_corpus_snapshot.py tests/test_evidence_graph_builder.py tests/test_generation_catalog.py tests/test_doctor.py -q`.
- [ ] Run Task 4: `uv run pytest tests/test_evidence_graph.py tests/test_evidence_graph_builder.py tests/test_evidence_graph_recovery.py tests/test_generation_catalog.py -q`.
- [ ] Run Task 3: `uv run pytest tests/test_code_intelligence.py tests/test_code_kernel_helpers.py -q`.
- [ ] Run `uv run ruff check scripts/ tests/` and `git diff --check`.
- [ ] Inspect `git status`, `git diff`, and recent log; stage only intended files.
- [ ] Commit once as `feat: seal analyzer workspaces on Windows` and report the SHA, test evidence, and any honestly unavailable platform primitive.
