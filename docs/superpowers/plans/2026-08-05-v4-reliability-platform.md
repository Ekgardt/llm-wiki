# V4 Reliability Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close baseline findings B1-B9 with correct cross-platform file identity, explicit SQLite connection ownership, deterministic MCP timeout tests, fail-closed metadata publication, and reproducible locked dependency environments.

**Architecture:** Repair the smallest shared boundaries before changing their callers. Path-to-descriptor checks compare identity, kind, size, and mtime while descriptor-to-descriptor checks also compare ctime; Windows identity comes only from `FILE_ID_INFO`; every SQLite connection has an explicit close owner; durable publication dispatches to strict POSIX sync or checked `FlushFileBuffers` plus `MoveFileExW(MOVEFILE_WRITE_THROUGH)`; production, optional, and development dependency profiles are separately declared and tested.

**Tech Stack:** Python 3.10-3.14, SQLite rollback journal, `ctypes` Win32 APIs, pytest, uv 0.12.1, MCP Python SDK 1.29.x, GitHub Actions, Bash, and Windows PowerShell 5.1-compatible syntax.

---

## Scope And Guardrails

- This plan implements only baseline findings B1-B9 and their platform, dependency, installer, CI, and documentation gates.
- Preserve Markdown authority, the three-zone layout, the existing 12 MCP tool names, rollback-journal SQLite, and the local no-daemon model.
- Do not add a runtime directory, database path, environment variable, MCP tool, release, or version bump.
- Do not reinterpret a failed durability operation as success. The stable failure code is `metadata_durability_unavailable`.
- Do not weaken race checks to make tests pass. File content hashes remain byte authority; metadata remains a race fence.
- Do not remove the `mcp-server` extra name. It becomes a compatibility alias after MCP moves into the production baseline.
- Do not commit, stage, amend, push, or create a PR unless the operator explicitly requests it. Each task stops at a green, reviewable diff.
- Preserve unrelated pre-existing worktree changes. Stage only files from a requested commit candidate.

## Finding Map

| Finding | Closure task | Required proof |
|---|---|---|
| B1 | Tasks 1-2 | Path/descriptor ctime-domain tests and Windows Python 3.10-3.14 execution |
| B2 | Task 3 | Full 64-bit volume plus 128-bit `FILE_ID_INFO` comparison |
| B3 | Task 9 | Direct development `jsonschema` declaration and clean schema tests |
| B4 | Task 9 | Direct hybrid/development NumPy declaration and clean vector smoke |
| B5 | Task 9 | Direct code-graph/development Jedi and tree-sitter declarations and clean parser smoke |
| B6 | Tasks 9-10 | Direct production MCP declaration and no-dev stdio initialize/list-tools smoke |
| B7 | Tasks 4-5 | Explicit close tests plus immediate Windows replace/unlink proof |
| B8 | Task 6 | Event-driven timeout tests, worker drain assertion, and pollution sentinel |
| B9 | Tasks 7-8 | Checked Win32 flush/move failures and old/new/duplicate publication states |

## Current-Source Decisions

The following decisions were verified against current sources on 2026-08-05:

- Python 3.14.7 documents Windows `st_ctime` as creation time and exposes `st_birthtime`; cross-domain ctime equality is therefore not a valid path/descriptor invariant. Source: https://docs.python.org/3.14/library/os.html#os.stat_result.st_ctime
- Python 3.14.7 documents that a `sqlite3.Connection` context manager commits or rolls back but does not close the connection. Source: https://docs.python.org/3.14/library/sqlite3.html#how-to-use-the-connection-context-manager
- Microsoft defines `FILE_ID_INFO` as a 64-bit volume serial plus a 128-bit file identifier, and requires both to compare open-file identity. Source: https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info
- Microsoft documents `FlushFileBuffers` as the checked file-buffer flush and `MOVEFILE_WRITE_THROUGH` as waiting for a move to be flushed before return. A checked writable-directory flush is fail-closed compatibility evidence for existing callers, not a claim of the stronger portable parent-directory `fsync` guarantee available on POSIX. Sources: https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers and https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw
- uv exact sync removes extraneous packages, `--inexact` retains them, `--no-default-groups` excludes development groups, `--locked` rejects a stale lock, and `--no-sync` prevents run-time environment mutation. Source: https://docs.astral.sh/uv/concepts/projects/sync/
- uv 0.12.1 is the current stable release, published 2026-07-31. Source: https://github.com/astral-sh/uv/releases/tag/0.12.1
- MCP 1.29.0 is the maintained v1 line, supports Python 3.10-3.14, and explicitly recommends `<2` for existing v1 deployments; MCP 2 uses a changed API. Its tagged source exports `ClientSession` and `StdioServerParameters`, whose `cwd` field accepts `str | Path | None`. Sources: https://pypi.org/project/mcp/1.29.0/ and https://github.com/modelcontextprotocol/python-sdk/blob/v1.29.0/src/mcp/client/stdio/__init__.py
- NumPy 2.2.6 supports Python 3.10 while current NumPy 2.5.x requires Python 3.12. Use `numpy>=2.2.6,<3` and let the universal lock choose compatible wheels. Source: https://pypi.org/project/numpy/2.2.6/
- GitHub recommends full commit SHA action pins. Preserve the already-qualified action majors in this repair to avoid coupling supply-chain pinning to unrelated action-runtime migrations. Source: https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions#using-third-party-actions
- Explicit stable runner labels are `ubuntu-24.04`, `windows-2025`, and `macos-15`; avoid mutable `-latest` aliases. Source: https://docs.github.com/en/actions/reference/runners/github-hosted-runners

## File Responsibility Map

| File | Responsibility in this plan |
|---|---|
| `scripts/markdown_transaction.py` | Metadata-domain comparison, closing coordinator connections, and durable Windows name publication |
| `scripts/code_workspace.py` | Metadata-domain comparison and full Windows directory identity use |
| `scripts/generation_catalog.py` | Canonical `FILE_ID_INFO` conversion and existing catalog close boundaries |
| `scripts/reliable_memory.py` | Stable durability exception, strict directory sync, and one durable file-publication primitive |
| `scripts/windows_workspace.py` | Typed `FlushFileBuffers` and `MoveFileExW` wrappers |
| `scripts/memory_state.py` | Route state-file replacement through durable publication |
| `scripts/memory_queue.py` | Closing queue connection factory |
| `scripts/archive_daily.py` | Close receipt and transaction read connections |
| `scripts/evidence_graph_builder.py` | Close incremental parent read connection |
| `scripts/search_memory.py` | Close legacy, evidence, and FTS read connections |
| `scripts/doctor.py` | Close direct catalog read connections |
| `scripts/install_smoke.py` | Production-only import, Doctor JSON, and stdio MCP smoke gate |
| `scripts/sync_memory.py` | Locked inexact production-baseline repair without development groups |
| `pyproject.toml` and `uv.lock` | Direct dependency ownership and uv version contract |
| `install.sh` and `install.ps1` | Pinned uv bootstrap, profile-correct sync, and bounded smoke execution |
| `.github/workflows/tests.yml` | Pinned actions, explicit runners, Python matrix, and clean-profile evidence |
| `tests/test_markdown_transaction.py` | B1 and B9 transaction regressions |
| `tests/test_code_workspace.py` | B1 and B2 regressions |
| `tests/test_reliable_memory.py` | B9 primitive state and failure regressions |
| `tests/test_windows_workspace.py` | Checked native API regressions |
| `tests/test_generation_catalog.py` | Catalog lifetime and Windows replace regressions |
| `tests/test_search_ranking.py` | Legacy/generation SQLite lifetime regressions |
| `tests/test_evidence_graph_builder.py` | Parent database lifetime regression |
| `tests/test_archive_daily_bagit.py` | Archive query lifetime regression |
| `tests/test_generation_maintenance.py` | Doctor catalog lifetime regression |
| `tests/test_memory_queue.py` | Queue connection lifetime regression |
| `tests/test_mcp_server.py` | B8 deterministic timeout cleanup |
| `tests/test_dependency_contract.py` | Direct dependency ownership and compatibility aliases |
| `tests/test_install_smoke.py` | Production smoke behavior without pytest |
| `tests/test_sync_memory.py` | Locked production rerun semantics |
| `tests/test_integration_injection.py` | Installer command and exit-status behavior |
| `tests/test_ci_policy.py` | Full SHA pins, explicit runners, timeouts, and clean lanes |
| `README.md`, `README.ru.md`, `README.zh-CN.md`, `integrations/README.md`, `CONTRIBUTING.md`, `CHANGELOG.md` | Synchronized operator contract and Unreleased repair record |

### Task 1: Separate Markdown Path And Descriptor Metadata Domains (B1)

**Files:**
- Modify: `tests/test_markdown_transaction.py:94-152,1179-1237`
- Modify: `scripts/markdown_transaction.py:3039-3169,3232-3318`

- [ ] **Step 1: Add focused failing comparator tests**

Add a stat fixture that keeps identity, kind, size, and mtime equal while changing only ctime:

```python
def _snapshot_stat(metadata: os.stat_result, *, ctime_ns: int) -> SimpleNamespace:
    return SimpleNamespace(
        st_dev=metadata.st_dev,
        st_ino=metadata.st_ino,
        st_mode=metadata.st_mode,
        st_size=metadata.st_size,
        st_mtime_ns=metadata.st_mtime_ns,
        st_ctime_ns=ctime_ns,
        st_file_attributes=getattr(metadata, "st_file_attributes", 0),
    )


def test_snapshot_comparison_ignores_ctime_only_across_metadata_domains(tmp_path: Path):
    target = tmp_path / "page.md"
    target.write_bytes(b"content")
    metadata = target.stat(follow_symlinks=False)
    path_stat = _snapshot_stat(metadata, ctime_ns=1)
    descriptor_stat = _snapshot_stat(metadata, ctime_ns=2)

    assert MarkdownCoordinator._same_capture_snapshot(
        path_stat, descriptor_stat, compare_ctime=False
    )
    assert not MarkdownCoordinator._same_capture_snapshot(
        path_stat, descriptor_stat, compare_ctime=True
    )
```

Add a behavioral test that monkeypatches only path `lstat` ctime and confirms `_read_bounded_target()` still returns exact bytes while identity/size/mtime remain equal. Add a second test that changes descriptor ctime between descriptor snapshots and confirms the read is rejected.

- [ ] **Step 2: Run the new tests and verify red**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_markdown_transaction.py -k "metadata_domains or path_descriptor or descriptor_snapshot" -q
```

Expected: FAIL because `_same_capture_snapshot()` has no `compare_ctime` parameter and always compares ctime.

- [ ] **Step 3: Add the explicit comparison mode**

Implement this exact signature and field split:

```python
@classmethod
def _same_capture_snapshot(
    cls,
    left: os.stat_result,
    right: os.stat_result,
    *,
    compare_ctime: bool,
) -> bool:
    stable = cls._same_capture_identity(left, right) and (
        stat.S_IFMT(left.st_mode),
        left.st_size,
        left.st_mtime_ns,
    ) == (
        stat.S_IFMT(right.st_mode),
        right.st_size,
        right.st_mtime_ns,
    )
    return stable and (
        not compare_ctime or left.st_ctime_ns == right.st_ctime_ns
    )
```

Use `compare_ctime=False` at path/descriptor boundaries after final `lstat`/`stat` revalidation. Use `compare_ctime=True` only for `fstat`-before versus `fstat`-after checks in `_read_stable_descriptor()` and `_hash_stable_descriptor()`.

- [ ] **Step 4: Run the focused and transaction regression tests**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_markdown_transaction.py::test_prepare_and_apply_create_replace_delete tests/test_markdown_transaction.py -k "snapshot or bounded_target or hash_bounded" -q
```

Expected: PASS. The create/replace/delete transaction must keep exact content and race checks.

- [ ] **Step 5: Stop at a reviewable checkpoint**

Run `git diff --check`. Do not stage or commit unless the operator explicitly requests a commit.

### Task 2: Apply The Same Metadata-Domain Rule To Code Capture (B1)

**Files:**
- Modify: `tests/test_code_workspace.py:571-583,769-809,1646-1799`
- Modify: `scripts/code_workspace.py:235-259,436-540,1646-1781`

- [ ] **Step 1: Add failing path/descriptor and descriptor/descriptor tests**

Add:

```python
def test_same_file_compares_ctime_only_inside_descriptor_domain() -> None:
    left = SimpleNamespace(
        st_dev=11,
        st_ino=22,
        st_mode=stat.S_IFREG | 0o600,
        st_size=7,
        st_mtime_ns=33,
        st_ctime_ns=44,
    )
    right = SimpleNamespace(**{**left.__dict__, "st_ctime_ns": 55})

    assert code_workspace._same_file(left, right, compare_ctime=False)
    assert not code_workspace._same_file(left, right, compare_ctime=True)
```

Keep `test_same_file_fails_closed_without_device_or_inode()` and make it call both modes so zero identity remains rejected.

- [ ] **Step 2: Run the new tests and verify red**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_code_workspace.py -k "same_file and (ctime or device)" -q
```

Expected: FAIL because `_same_file()` always compares ctime and has no metadata-domain argument.

- [ ] **Step 3: Implement the explicit code-capture comparator**

Use this signature:

```python
def _same_file(
    left: os.stat_result,
    right: os.stat_result,
    *,
    compare_ctime: bool,
) -> bool:
```

The body must fail closed for zero/nonpositive device or inode, compare `(st_dev, st_ino)`, file kind, size, and mtime in every mode, and compare ctime only when `compare_ctime=True`.

Use `compare_ctime=False` for enumeration/path metadata versus an opened descriptor in `_read_candidate()`, `_entry_identity()`, and POSIX directory traversal. Use `compare_ctime=True` for the two `fstat()` snapshots in `_verify_descriptor_file()`.

- [ ] **Step 4: Run deterministic capture and race suites**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_code_workspace.py::test_repository_contracts_are_frozen_slotted_normalized_and_deterministic tests/test_code_workspace.py -k "replacement_between_stat or capture_persists_stat or verify_descriptor" -q
```

Expected: PASS with no reduction in replacement/race rejection.

- [ ] **Step 5: Stop at a reviewable checkpoint**

Run `git diff --check`. Do not stage or commit unless explicitly requested.

### Task 3: Use Full FILE_ID_INFO For Windows Identity (B2)

**Files:**
- Modify: `tests/test_code_workspace.py:812-935,1019-1033`
- Modify: `scripts/generation_catalog.py:35-140,539-582,620-643`
- Modify: `scripts/code_workspace.py:497-531`

- [ ] **Step 1: Replace legacy-width tests with full-identity tests**

Replace tuple-of-two tests with the canonical shape:

```python
def test_windows_stat_identity_comparison_uses_full_file_id_info() -> None:
    volume = 0x94A0A7748E750B60
    inode = (1 << 96) + 0x000D00000011B0F7
    file_id = inode.to_bytes(16, "little")
    metadata = SimpleNamespace(st_dev=volume, st_ino=inode)
    identity = ("windows", volume, file_id)

    assert generation_catalog._windows_stat_matches_identity(metadata, identity)
    assert not generation_catalog._windows_stat_matches_identity(
        metadata, ("windows", volume & 0xFFFFFFFF, file_id)
    )
    assert not generation_catalog._windows_stat_matches_identity(
        metadata, ("windows", volume, file_id[::-1])
    )
```

Add a real-Windows test that opens a directory handle, obtains `_windows_handle_file_identity(handle)`, reads `path.stat(follow_symlinks=False)`, and asserts `_windows_stat_matches_identity()` succeeds on Python 3.10-3.14.

- [ ] **Step 2: Run the identity tests and verify red**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_code_workspace.py -k "windows_stat_identity or directory_handle_identity" -q
```

Expected: FAIL because production still compares Python identity with legacy `BY_HANDLE_FILE_INFORMATION` widths.

- [ ] **Step 3: Make FILE_ID_INFO the only identity source**

Implement:

```python
def _windows_stat_matches_identity(
    metadata: os.stat_result,
    identity: tuple[str, int, bytes],
) -> bool:
    if (
        len(identity) != 3
        or identity[0] != "windows"
        or type(identity[1]) is not int
        or identity[1] <= 0
        or type(identity[2]) is not bytes
        or len(identity[2]) != 16
        or not any(identity[2])
    ):
        return False
    device = getattr(metadata, "st_dev", 0)
    inode = getattr(metadata, "st_ino", 0)
    if type(device) is not int or type(inode) is not int or device <= 0 or inode <= 0:
        return False
    return (device, inode) == (
        identity[1],
        int.from_bytes(identity[2], "little", signed=False),
    )
```

In `_entry_identity()`, query `_windows_handle_file_identity()` once, compare that same tuple with the path stat, then return it. Remove `_windows_handle_stat_identity()`.

Replace `_ByHandleFileInformation` attribute checks with `GetFileInformationByHandleEx(FileAttributeTagInfo)` and a focused `_windows_handle_attributes()` helper. Remove the legacy `GetFileInformationByHandle` binding after its final caller is gone.

- [ ] **Step 4: Run Windows capture and handle-leak tests**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_code_workspace.py -k "windows and (identity or replacement or handle)" -q
```

Expected: PASS. Handle counts must return to baseline on success and failure.

- [ ] **Step 5: Run the broader identity consumers**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_code_workspace.py tests/test_generation_catalog.py tests/test_corpus_snapshot.py -q
```

Expected: PASS.

- [ ] **Step 6: Stop at a reviewable checkpoint**

Run `git diff --check`. Do not stage or commit unless explicitly requested.

### Task 4: Give Coordinator And Queue Connections Closing Owners (B7)

**Files:**
- Modify: `tests/test_markdown_transaction.py`
- Modify: `tests/test_memory_queue.py`
- Modify: `scripts/markdown_transaction.py:890-905`
- Modify: `scripts/memory_queue.py:380-397`

- [ ] **Step 1: Add failing connection-closure tests**

For each factory, retain the yielded connection and prove it is unusable after context exit:

```python
def _assert_connection_closed(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_markdown_connect_context_commits_and_closes(vault: Path, state_root: Path):
    coordinator = MarkdownCoordinator(vault, state_root)
    with coordinator._connect() as database:
        assert database.execute("SELECT 1").fetchone()[0] == 1
    _assert_connection_closed(database)
```

Add the equivalent `test_queue_connect_context_commits_and_closes()` using `MemoryQueue`. Add one rollback assertion per factory so adding close ownership does not change transaction semantics.

- [ ] **Step 2: Run the tests and verify red**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_markdown_transaction.py tests/test_memory_queue.py -k "connect_context" -q
```

Expected: FAIL because `Connection.__exit__()` leaves the connection open.

- [ ] **Step 3: Convert both factories to context managers**

Use this ownership shape in `MarkdownCoordinator._connect()`:

```python
@contextlib.contextmanager
def _connect(
    self,
    *,
    busy_ms: int | None = None,
) -> Iterator[sqlite3.Connection]:
    database = open_operational_db(
        self.database_path,
        busy_ms=DEFAULTS.markdown_busy_ms if busy_ms is None else busy_ms,
    )
    try:
        schema = database.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'transaction'"
        ).fetchone()
        if schema is not None and "conflicted" not in (schema["sql"] or ""):
            database.execute("PRAGMA ignore_check_constraints = ON")
        with database:
            yield database
    finally:
        database.close()
```

Use the same `try -> with connection -> yield -> finally close` shape in `MemoryQueue._connect()`. Preserve the existing owner-only hardening and close immediately if hardening fails.

- [ ] **Step 4: Run transaction semantics and lifetime tests**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_markdown_transaction.py tests/test_memory_queue.py -k "connect_context or begin_immediate or rollback or commit" -q
```

Expected: PASS. Explicit `begin_immediate()` remains the inner write transaction and each outer factory closes exactly once.

- [ ] **Step 5: Stop at a reviewable checkpoint**

Run `git diff --check`. Do not stage or commit unless explicitly requested.

### Task 5: Close Every Direct SQLite Read Path (B7)

**Files:**
- Modify: `scripts/archive_daily.py:224-252,1016-1040`
- Modify: `scripts/evidence_graph_builder.py:1019-1068`
- Modify: `scripts/search_memory.py:860-906,1549-1666,1669-1700`
- Modify: `scripts/doctor.py:3735-3781`
- Modify: `tests/test_archive_daily_bagit.py`
- Modify: `tests/test_evidence_graph_builder.py`
- Modify: `tests/test_search_ranking.py`
- Modify: `tests/test_generation_maintenance.py`

- [ ] **Step 1: Add failing close-boundary regressions**

Add tests with a tracking wrapper whose transaction `__exit__` deliberately does not mark the connection closed:

```python
class TrackingConnection:
    def __init__(self, database: sqlite3.Connection) -> None:
        self.database = database
        self.closed = False

    def __getattr__(self, name: str):
        return getattr(self.database, name)

    def __enter__(self):
        return self

    def __exit__(self, *args: object):
        return self.database.__exit__(*args)

    def close(self) -> None:
        self.closed = True
        self.database.close()
```

Cover these exact operations:

- `ArchiveManager._receipt_operation_state()` and `_transaction_references()`.
- `evidence_graph_builder._parent_records()`.
- `search_memory._needs_rebuild()`.
- `search_memory._generation_authoritative_sources()`.
- `search_memory.validate_generation_fts_artifact()`.
- `doctor._repair_generation_catalog()` direct `_readonly()` calls.

After each operation, assert every tracked connection is closed. On Windows, also replace or unlink the SQLite file immediately after return; this is the behavioral proof that no handle survives.

- [ ] **Step 2: Run the new tests and verify red**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_archive_daily_bagit.py tests/test_evidence_graph_builder.py tests/test_search_ranking.py tests/test_generation_maintenance.py -k "closes_connection or releases_database or sqlite_lifetime" -q
```

Expected: FAIL for direct `with sqlite3.connect(...)` and `with catalog._readonly()` sites.

- [ ] **Step 3: Add explicit closing boundaries**

Import `closing` from `contextlib` in each affected module. Replace each direct connection context with:

```python
with closing(sqlite3.connect(uri, uri=True, timeout=0)) as database:
    database.row_factory = sqlite3.Row
    rows = database.execute(statement).fetchall()
```

For a write transaction that also needs commit/rollback behavior, nest it:

```python
with closing(sqlite3.connect(path)) as database, database:
    database.execute(statement, parameters)
```

For Doctor catalog reads, use `with closing(catalog._readonly()) as database:`. Do not change `GenerationCatalog` methods already using `closing(...)`, and do not close connections returned intentionally by `_generation_connection()` because their callers own and close them.

- [ ] **Step 4: Run focused lifetime tests**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_generation_catalog.py::test_catalog_explicitly_closes_every_opened_connection tests/test_archive_daily_bagit.py tests/test_evidence_graph_builder.py tests/test_search_ranking.py tests/test_generation_maintenance.py -k "close or replace or parent_records or needs_rebuild or generation_fts" -q
```

Expected: PASS, including Windows replace/unlink assertions.

- [ ] **Step 5: Run all affected suites**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_generation_catalog.py tests/test_archive_daily_bagit.py tests/test_evidence_graph_builder.py tests/test_search_ranking.py tests/test_generation_maintenance.py tests/test_doctor.py -q
```

Expected: PASS.

- [ ] **Step 6: Stop at a reviewable checkpoint**

Run `git diff --check`. Do not stage or commit unless explicitly requested.

### Task 6: Drain MCP Timeout Workers Before Test Teardown (B8)

**Files:**
- Modify: `tests/test_mcp_server.py:1598-1635,2977-3040`
- Production code: no change expected

- [ ] **Step 1: Replace elapsed sleeps with explicit start and drain events**

In `test_blocking_tool_does_not_block_event_loop_and_times_out`, create the worker function inside the coroutine so it can signal an `asyncio.Event` safely:

```python
class DrainAwareSet(set):
    def __init__(self, drained: threading.Event) -> None:
        super().__init__()
        self.drained = drained

    def discard(self, value: object) -> None:
        super().discard(value)
        if not self:
            self.drained.set()


async def exercise():
    loop = asyncio.get_running_loop()
    started = asyncio.Event()

    def blocked(*, deadline):
        loop.call_soon_threadsafe(started.set)
        try:
            release.wait(1.0)
        finally:
            finished.set()
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "_vault_status", blocked)
    task = asyncio.create_task(mcp_server._handle_tool_call("vault_status", {}))
    await asyncio.wait_for(started.wait(), timeout=0.5)
    loop_progressed = not finished.is_set()
    return loop_progressed, await task
```

Use `DrainAwareSet` as `_MCP_WORKERS`. In `finally`, set `release`, then assert `finished.wait(1.0)` and `drained.wait(1.0)` both return `True`. Under `_MCP_WORKERS_LOCK`, assert the worker set is empty before the monkeypatch fixture can restore module globals.

- [ ] **Step 2: Apply the same event protocol to registered resources**

Update `test_registered_resources_share_one_deadline_and_run_off_loop` so both parameterized URIs wait on an `asyncio.Event`, release the blocked helper, wait for worker completion, wait for registry drain, and assert an empty registry before return. Do not use `asyncio.sleep()` or wall-clock ordering as a synchronization mechanism.

- [ ] **Step 3: Add a pollution sentinel**

Keep the pollution sentinel inside each existing timeout test so it cannot depend on test ordering. Immediately after the drain assertion, replace the blocked helper with a fresh nonblocking helper, repeat the same tool or resource callback, parse its envelope, and assert it contains the helper's normal data rather than `operation_timeout`. The assertion must fail if a late callback mutates restored globals or consumes a worker slot.

- [ ] **Step 4: Run the two tests repeatedly and verify stability**

Run:

```powershell
1..25 | ForEach-Object { uv run --locked --no-sync pytest tests/test_mcp_server.py::TestHandleToolCall::test_blocking_tool_does_not_block_event_loop_and_times_out tests/test_mcp_server.py::TestResources::test_registered_resources_share_one_deadline_and_run_off_loop -q; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }
```

Expected: all 25 iterations PASS for the exact collected node IDs above, including both parameterized resource URIs, with no pending worker output after pytest returns.

- [ ] **Step 5: Run the whole MCP suite**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_mcp_server.py -q
```

Expected: PASS. Production `mcp_server.py` remains unchanged unless the new sentinel proves a real registry defect.

- [ ] **Step 6: Stop at a reviewable checkpoint**

Run `git diff --check`. Do not stage or commit unless explicitly requested.

### Task 7: Add Checked Windows Flush And Write-Through Move Wrappers (B9)

**Files:**
- Modify: `tests/test_windows_workspace.py`
- Modify: `scripts/windows_workspace.py:41-279,768-853`

- [ ] **Step 1: Add failing native wrapper tests**

Add Windows-only tests for these contracts:

- `flush_file_path(path: Path) -> None` opens the exact local regular file, rejects reparse points, calls `FlushFileBuffers`, checks its return value, and closes the handle on every path.
- `move_file_write_through(source: Path, destination: Path, *, replace: bool) -> None` always sets `MOVEFILE_WRITE_THROUGH`, sets `MOVEFILE_REPLACE_EXISTING` only for replace, never sets `MOVEFILE_COPY_ALLOWED`, and raises the exact Win32 error on failure.
- A create-only move refuses an existing destination.
- A replace move leaves the destination with exact source bytes.

Use a fake `_API` in unit tests to assert flags, then retain one real-Windows test for actual bytes and handle closure.

- [ ] **Step 2: Run the tests and verify red**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_windows_workspace.py -k "write_through or flush_file_path" -q
```

Expected: FAIL because `MoveFileExW` is not bound and neither public wrapper exists.

- [ ] **Step 3: Bind the APIs with exact ctypes signatures**

Add `MoveFileExW` to `_WindowsApi.required` and bind:

```python
self.move_file_ex = kernel32.MoveFileExW
self.move_file_ex.argtypes = (
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
)
self.move_file_ex.restype = wintypes.BOOL
```

Define `_MOVEFILE_REPLACE_EXISTING = 0x1` and `_MOVEFILE_WRITE_THROUGH = 0x8`. Do not enable cross-volume copy.

Implement and export:

```python
def flush_file_path(path: Path) -> None:
    """Flush one bounded local regular file through a checked Win32 handle."""


def move_file_write_through(
    source: Path,
    destination: Path,
    *,
    replace: bool,
) -> None:
    """Move one local name and wait for the checked Win32 move to flush."""
```

Both functions use `_bounded_local_absolute_path()`. `flush_file_path()` opens with write access and `FILE_FLAG_OPEN_REPARSE_POINT`, verifies non-reparse regular-file identity, invokes `flush_file()`, and closes in `finally`. `move_file_write_through()` passes only write-through plus the conditional replace flag and raises `ctypes.WinError(ctypes.get_last_error())` when the call returns false.

- [ ] **Step 4: Run native and existing workspace tests**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_windows_workspace.py tests/test_code_workspace.py -k "windows_workspace or write_through or flush" -q
```

Expected: PASS on Windows; non-Windows import tests remain green and native tests skip honestly.

- [ ] **Step 5: Stop at a reviewable checkpoint**

Run `git diff --check`. Do not stage or commit unless explicitly requested.

### Task 8: Build One Fail-Closed Durable Publication Primitive (B9)

**Files:**
- Modify: `tests/test_reliable_memory.py:181-200`
- Modify: `tests/test_markdown_transaction.py:1179-1237`
- Modify: `tests/test_memory_state_permissions.py`
- Modify: `scripts/reliable_memory.py:55-63,469-493`
- Modify: `scripts/markdown_transaction.py:824-855,3642-3690`
- Modify: `scripts/memory_state.py:142-147,263-273`

- [ ] **Step 1: Add failing strict-sync and publication-state tests**

Add this stable exception assertion:

```python
def test_metadata_durability_error_has_stable_code() -> None:
    error = reliable_memory.MetadataDurabilityUnavailable("flush failed")
    assert error.code == "metadata_durability_unavailable"
```

Add table-driven tests for the publication state machine:

| Staged path | Destination path | Expected result |
|---|---|---|
| Expected bytes | Absent or old bytes | Publish and return `published` |
| Absent | Expected bytes | Return `adopted` after stable read-back |
| Expected bytes | Expected bytes | Return `duplicate` without deleting either name |
| Missing/wrong staged bytes | Missing/wrong destination bytes | Raise a conflict; never claim success |

Inject these exact failures:

- POSIX parent `fsync` raises `EINVAL`/`ENOTSUP` after rename: raise `MetadataDurabilityUnavailable`; destination may be new and retry must return `adopted`.
- POSIX create-only hard link succeeds and staged unlink fails: raise, retain both names, and the observer returns `duplicate`.
- Windows `FlushFileBuffers` fails before move: raise with old destination and staged evidence intact.
- Windows `MoveFileExW` fails: raise with old destination and staged evidence intact.
- Windows move succeeds and a simulated caller crash happens before acknowledgement: retry with absent staged and expected destination returns `adopted`.

- [ ] **Step 2: Run the tests and verify red**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_reliable_memory.py tests/test_windows_workspace.py -k "metadata_durability or durable_publish" -q
```

Expected: FAIL because the exception, observer, and primitive do not exist and unsupported directory sync is currently swallowed.

- [ ] **Step 3: Define the stable API**

Add `Literal` to the existing `typing` import, then add:

```python
class MetadataDurabilityUnavailable(OSError):
    """Raised when the platform cannot prove the requested metadata boundary."""

    code = "metadata_durability_unavailable"


def durable_publish_file(
    staged: Path,
    destination: Path,
    *,
    replace: bool,
    expected_sha256: str,
    max_bytes: int,
) -> Literal["published", "adopted", "duplicate"]:
    """Publish one sibling file and return published, adopted, or duplicate."""
```

The implementation must enforce all of these invariants:

- `staged` and `destination` are distinct sibling paths under one resolved local parent.
- `expected_sha256` is lowercase 64-hex and `max_bytes` is a positive non-boolean integer.
- Every existing file is read through a no-follow descriptor, bounded before allocation, checked before/after for identity, kind, size, and mtime, and hashed from the descriptor.
- If destination already has expected bytes and staged is absent, return `adopted`.
- If both names have expected bytes, return `duplicate` and retain both for caller-specific reconciliation.
- If staged does not have expected bytes, fail before any name mutation.
- POSIX flushes staged content, uses `link` for create-only or `replace` for replacement, and requires successful parent-directory `fsync` after the final name mutation.
- Windows calls `windows_workspace.flush_file_path()` then `move_file_write_through()` with the requested replace mode.
- A failed/unsupported flush or write-through move raises `MetadataDurabilityUnavailable` from the native error. `FileExistsError` remains a create conflict, not a durability success.
- Read back and rehash destination before returning `published`; no unchecked success path exists.

- [ ] **Step 4: Make `fsync_directory()` fail closed**

On POSIX, stop suppressing `EBADF`, `EINVAL`, and `ENOTSUP`; wrap unsupported/failure in `MetadataDurabilityUnavailable`. On Windows, use `open_writable_directory_path()`, call checked `flush_directory()`, raise `MetadataDurabilityUnavailable` from `ctypes.WinError(ctypes.get_last_error())` when it returns false, and always close the handle. This preserves existing callers while removing the silent Windows return. Treat a successful call only as checked Windows compatibility evidence; the publication guarantee remains the file `FlushFileBuffers` plus `MoveFileExW(MOVEFILE_WRITE_THROUGH)` contract and fail-closed recovery, not POSIX-equivalent parent-directory durability.

Change `test_posix_directory_fsync_unsupported_error_is_tolerated` to `test_posix_directory_fsync_unsupported_is_durability_failure` and assert the stable error code.

- [ ] **Step 5: Route current authoritative replacements through the primitive**

In `MarkdownCoordinator._apply_windows_operation()`, replace `_rename_windows_handle()` for create/replace with `durable_publish_file()` using `row["after_hash"]` and `MAX_KNOWLEDGE_TARGET_BYTES`. Keep the held parent handle until read-back verification completes. Remove `_rename_windows_handle()` after its last caller and update writer-scanner allowlists/tests accordingly.

In `memory_state.atomic_write()`, encode first, write one unique sibling staging file with exclusive creation, and call `durable_publish_file(staged, path, replace=True, expected_sha256=sha256_bytes(payload), max_bytes=max(1, len(payload)))`. Make `save_state()` call `atomic_write()` so `run/state.json` no longer bypasses the durability boundary. A failed publication retains recoverable staging evidence and raises; it must not silently overwrite a corrupt state file.

- [ ] **Step 6: Run focused transaction and state tests**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_reliable_memory.py tests/test_windows_workspace.py tests/test_markdown_transaction.py tests/test_memory_state_permissions.py -k "durab or fsync or write_through or create_replace_delete or atomic_write or save_state" -q
```

Expected: PASS, including old/new/duplicate states and injected native failures.

- [ ] **Step 7: Run all existing durability consumers**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_archive_daily_bagit.py tests/test_build_tiers.py tests/test_compile_cache.py tests/test_evidence_graph_builder.py tests/test_generation_catalog.py tests/test_memory_queue.py tests/test_search_ranking.py -q
```

Expected: PASS. Any platform that cannot perform a required sync must fail with `metadata_durability_unavailable`, not report success.

- [ ] **Step 8: Stop at a reviewable checkpoint**

Run `git diff --check`. Do not stage or commit unless explicitly requested.

### Task 9: Declare Direct Dependency Ownership And Pin uv (B3-B6)

**Files:**
- Create: `tests/test_dependency_contract.py`
- Modify: `pyproject.toml:1-59`
- Modify: `uv.lock`

- [ ] **Step 1: Add failing dependency ownership tests**

Parse `pyproject.toml` with `tomllib`/`tomli` and assert:

```python
def test_production_baseline_owns_mcp_v1_and_python310_tomli(project: dict) -> None:
    dependencies = project["project"]["dependencies"]
    assert "mcp>=1.29,<2" in dependencies
    assert "tomli>=2.4.1,<3; python_version < '3.11'" in dependencies
    assert project["project"]["optional-dependencies"]["mcp-server"] == []


def test_hybrid_and_dev_profiles_own_their_imports(project: dict) -> None:
    hybrid = project["project"]["optional-dependencies"]["hybrid"]
    dev = project["dependency-groups"]["dev"]
    assert "numpy>=2.2.6,<3" in hybrid
    for requirement in (
        "jsonschema>=4.26,<5",
        "numpy>=2.2.6,<3",
        "jedi>=0.19,<1",
        "tree-sitter>=0.23,<1",
    ):
        assert requirement in dev


def test_uv_version_contract_is_exact(project: dict) -> None:
    assert project["tool"]["uv"]["required-version"] == "==0.12.1"
```

Also assert every tree-sitter grammar in the `code-graph` extra is a direct development requirement, so the documented full suite cannot depend on accidental transitive packages.

- [ ] **Step 2: Run the tests and verify red**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_dependency_contract.py -q
```

Expected: FAIL because production dependencies are empty, MCP is only an extra, and the dev group omits schema/vector/code-graph requirements.

- [ ] **Step 3: Update `pyproject.toml` ownership**

Use these declarations while preserving all unrelated extras:

```toml
[project]
dependencies = [
    "mcp>=1.29,<2",
    "tomli>=2.4.1,<3; python_version < '3.11'",
]

[project.optional-dependencies]
mcp-server = []
hybrid = [
    "numpy>=2.2.6,<3",
    "lancedb>=0.20",
    "sentence-transformers>=2.7,<6",
]

[dependency-groups]
dev = [
    "jsonschema>=4.26,<5",
    "numpy>=2.2.6,<3",
    "jedi>=0.19,<1",
    "tree-sitter>=0.23,<1",
    "tree-sitter-python>=0.23,<1",
    "tree-sitter-javascript>=0.23,<1",
    "tree-sitter-typescript>=0.23,<1",
    "tree-sitter-go>=0.23,<1",
    "tree-sitter-rust>=0.23,<1",
    "tree-sitter-java>=0.23,<1",
    "tree-sitter-c>=0.23,<1",
    "tree-sitter-cpp>=0.23,<1",
    "tree-sitter-ruby>=0.23,<1",
    "tree-sitter-php>=0.23,<1",
    "tree-sitter-c-sharp>=0.23,<1",
    "tree-sitter-bash>=0.23,<1",
    "pre-commit>=4.5.1",
    "pytest>=8.0.0",
    "pyyaml>=6.0.3,<7",
    "ruff>=0.6.0",
]

[tool.uv]
required-version = "==0.12.1"
```

The duplicate declarations between optional production extras and the dev group are intentional ownership, not accidental transitive reliance.

- [ ] **Step 4: Regenerate and check the universal lock**

Run with uv 0.12.1:

```powershell
uv lock
uv lock --check --no-python-downloads
```

Expected: both commands exit 0. Inspect `uv.lock` and confirm MCP remains `<2`, Python 3.10 resolves NumPy 2.2.6-compatible artifacts, and all direct requirements appear in the root package metadata.

- [ ] **Step 5: Run dependency ownership and feature tests**

Run:

```powershell
uv sync --locked
uv run --locked --no-sync pytest tests/test_dependency_contract.py tests/test_reliable_memory_schemas.py tests/test_generation_integration.py tests/test_code_extractor.py tests/test_code_graph.py tests/test_mcp_server.py -q
```

Expected: PASS without import-related skips for B3-B6 dependencies.

- [ ] **Step 6: Stop at a reviewable checkpoint**

Run `git diff --check`. Do not stage or commit unless explicitly requested.

### Task 10: Replace Installer Pytest With A Production Smoke Gate (B6)

**Files:**
- Create: `scripts/install_smoke.py`
- Create: `tests/test_install_smoke.py`
- Modify: `scripts/sync_memory.py:61-260`
- Modify: `tests/test_sync_memory.py:174-337,997-1142`
- Modify: `install.sh:120-255`
- Modify: `install.ps1:121-199`
- Modify: `tests/test_integration_injection.py:2138-2835,3620-3635`

- [ ] **Step 1: Add failing smoke-runner tests**

Test these exact public functions:

```python
def run_smoke(
    root: Path,
    state_root: Path,
    *,
    deadline_seconds: float = 120.0,
) -> dict[str, object]:
```

Required cases:

- Production imports include `mcp`, `mcp_server`, and `tomli` on Python 3.10; `mcp_server.MCP_AVAILABLE` is true.
- Doctor runs as `python scripts/doctor.py --json` without `--repair`; malformed JSON, timeout, return code 2, or `overall_status=error` fails.
- Doctor return code 1 with a schema-valid degraded report is retained as degraded evidence, not mislabeled healthy.
- A real stdio MCP client initializes the production server, lists tools, and gets exactly 12 unique names.
- One absolute 120-second monotonic deadline covers imports, Doctor, and MCP; each child receives only remaining time.
- Timeout/failure produces bounded stderr and exit code 1; success emits one JSON object and exit code 0.

- [ ] **Step 2: Run the tests and verify red**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_install_smoke.py -q
```

Expected: FAIL because `scripts/install_smoke.py` does not exist.

- [ ] **Step 3: Implement the production smoke**

Use the MCP SDK rather than hand-rolling JSON-RPC framing:

```python
async def _mcp_tools(root: Path, timeout: float) -> tuple[str, ...]:
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(root / "scripts" / "mcp_server.py")],
        cwd=str(root),
        env=os.environ.copy(),
    )
    with anyio.fail_after(timeout):
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                return tuple(tool.name for tool in result.tools)
```

Validate `len(names) == len(set(names)) == 12`. Run Doctor in a subprocess with `timeout=remaining`. Redact/bound error output before printing. The script must not import pytest and must not mutate knowledge or run Doctor repair.

- [ ] **Step 4: Change fresh and repeated sync semantics**

Fresh installer provisioning uses:

```text
uv sync --locked --no-default-groups --quiet
```

If `.venv` already exists, installer reruns use:

```text
uv sync --locked --no-default-groups --inexact --quiet
```

Change `sync_memory._dependency_action()` dry-run/apply commands to locked, inexact, no-default-groups production sync without `--extra mcp-server`. Preserve one aggregate deadline and process-tree cleanup.

- [ ] **Step 5: Pin installer uv bootstrap**

Set one visible `UV_VERSION=0.12.1`/`$UvVersion = "0.12.1"`. When uv is absent, download only the release-specific installer URLs:

```text
https://releases.astral.sh/github/uv/releases/download/0.12.1/uv-installer.sh
https://releases.astral.sh/github/uv/releases/download/0.12.1/uv-installer.ps1
```

After installation, require `uv --version` to report 0.12.1 before sync. A pre-existing mismatched uv fails with a direct upgrade instruction; do not silently proceed around `[tool.uv].required-version`.

- [ ] **Step 6: Replace full pytest invocation with the smoke command**

Keep the existing Bash process-group and PowerShell process-tree cleanup machinery, but execute:

```text
uv run --locked --no-sync python scripts/install_smoke.py --deadline-seconds 120
```

Rename installer tests from `pytest_exit_status` to `smoke_exit_status` and preserve injected nonzero, signal, stubborn-child, and immediate `$LASTEXITCODE` cases. Any smoke failure must exit nonzero and suppress the final success summary.

- [ ] **Step 7: Run installer and sync tests**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_install_smoke.py tests/test_sync_memory.py tests/test_integration_injection.py -k "smoke or dependenc or installer or sync_exit" -q
```

Expected: PASS.

- [ ] **Step 8: Run real production-profile smoke locally**

Use a disposable environment outside the workspace:

```powershell
$env:UV_PROJECT_ENVIRONMENT = Join-Path $env:TEMP "llm-wiki-prod-smoke"
uv sync --locked --no-default-groups
uv run --locked --no-sync python scripts/install_smoke.py --deadline-seconds 120
Remove-Item Env:UV_PROJECT_ENVIRONMENT
```

Expected: exit 0, structured JSON, exactly 12 tools, and no pytest requirement. Remove the disposable environment only after confirming no process owns it.

- [ ] **Step 9: Stop at a reviewable checkpoint**

Run `git diff --check`. Do not stage or commit unless explicitly requested.

### Task 11: Add Immutable CI And Clean Environment Evidence (B1-B6)

**Files:**
- Create: `tests/test_ci_policy.py`
- Modify: `tests/test_readme_i18n.py:145-159`
- Modify: `.github/workflows/tests.yml`

- [ ] **Step 1: Add failing parsed-workflow policy tests**

Parse YAML and assert behavior, not source substrings:

- Every non-local `uses:` value ends in a 40-character lowercase hex SHA.
- Checkout is `11bd71901bbe5b1630ceea73d27597364c9af683` (`v4.2.2`).
- setup-uv is `d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86` (`v5.4.2`) and every use sets `version: "0.12.1"`.
- setup-node is `49933ea5288caeca8642d1e84afbd3f7d6820020` (`v4.4.0`).
- Runner labels contain no `-latest`; use only `ubuntu-24.04`, `windows-2025`, and `macos-15`.
- Windows full-suite entries cover Python `3.10`, `3.11`, `3.12`, `3.13`, and `3.14`.
- Linux full-suite entries cover Python `3.10` and `3.14`.
- macOS confirms Python `3.10` and `3.14`.
- Windows full-suite timeout is 60 minutes; Linux/macOS full-suite timeout is 45; focused jobs are 15; clean-profile jobs are 20.
- A production clean lane runs `uv sync --locked --no-default-groups` and the real install smoke on Python 3.10 and 3.14.
- Hybrid and code-graph clean lanes exclude default groups and execute direct import/feature smoke without pytest.

- [ ] **Step 2: Run policy tests and verify red**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_ci_policy.py tests/test_readme_i18n.py::test_ci_qualifies_real_pyright_on_all_supported_os_families -q
```

Expected: FAIL on mutable action tags, `-latest` runners, missing Python 3.14, and absent clean lanes.

- [ ] **Step 3: Split CI by responsibility**

Create these jobs with `fail-fast: false` where matrixed:

| Job | Runner/Python | Timeout | Command contract |
|---|---|---:|---|
| `gitleaks` | Ubuntu 24.04 | 10 | Existing pinned gitleaks action |
| `lint` | Ubuntu 24.04 / 3.14 | 15 | Ruff over `scripts/ tests/ benchmark/`, structural lint, compileall, shell/Node syntax |
| `pytest-linux` | Ubuntu 24.04 / 3.10, 3.14 | 45 | Locked dev sync, full hermetic suite |
| `pytest-windows` | Windows 2025 / 3.10-3.14 | 60 | Locked dev sync, full hermetic suite including native B1/B2/B7/B9 tests |
| `pytest-macos` | macOS 15 / 3.10, 3.14 | 45 | Locked dev sync, full hermetic suite |
| `clean-production` | Ubuntu 24.04 / 3.10, 3.14 | 20 | No-default-groups sync, assert pytest absent, run install smoke |
| `clean-hybrid` | Ubuntu 24.04 / 3.10, 3.14 | 20 | No-default-groups plus hybrid; import NumPy and run vector smoke |
| `clean-code-graph` | Ubuntu 24.04 / 3.10, 3.14 | 20 | No-default-groups plus code-graph; import Jedi/core/all grammars and parse fixtures |
| `pyright-navigation` | Explicit Ubuntu/Windows/macOS / 3.10 | 15 | Existing real-Pyright qualification with shell-independent state-root argument |

Use `uv run --locked --no-sync` after every explicit sync. Do not let a clean lane gain pytest through default groups or `uv run` auto-sync.

- [ ] **Step 4: Pin all actions by full SHA**

Use the exact SHAs from Step 1 and retain the release tag as a same-line comment, for example:

```yaml
- uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
- uses: astral-sh/setup-uv@d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86 # v5.4.2
  with:
    version: "0.12.1"
    enable-cache: true
```

Keep `permissions: contents: read`. Do not broaden token permissions.

- [ ] **Step 5: Run workflow policy and YAML tests**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_ci_policy.py tests/test_readme_i18n.py -q
```

Expected: PASS.

- [ ] **Step 6: Run local equivalents of clean dependency lanes**

Run each with a separate disposable `UV_PROJECT_ENVIRONMENT`. Expected: production MCP smoke passes with pytest absent; hybrid imports NumPy and creates/reads vectors; code-graph imports Jedi, tree-sitter core, and all 12 grammars and parses fixture sources.

- [ ] **Step 7: Stop at a reviewable checkpoint**

Run `git diff --check`. Do not stage or commit unless explicitly requested.

### Task 12: Synchronize Operator Documentation And Changelog

**Files:**
- Modify: `README.md`
- Modify: `README.ru.md`
- Modify: `README.zh-CN.md`
- Modify: `integrations/README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `CHANGELOG.md:7-10`
- Modify: `tests/test_readme_i18n.py`

- [ ] **Step 1: Update parity tests first**

Change the shared install marker from the old MCP extra command to:

```text
uv sync --locked --no-default-groups
```

Add shared assertions for:

- MCP is part of the production baseline; `mcp-server` remains a compatibility alias.
- Production/unattended commands use `uv run --locked --no-sync`.
- Optional extras are additive with `uv sync --locked --no-default-groups --inexact --extra hybrid` or `uv sync --locked --no-default-groups --inexact --extra code-graph`.
- Development uses `uv sync --locked` and owns pytest/schema/vector/code-graph test dependencies.
- Node 22 remains optional and only needed for qualified precise Python navigation.
- The installer runs a bounded production smoke, not the full regression suite.

- [ ] **Step 2: Run parity tests and verify red**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_readme_i18n.py -q
```

Expected: FAIL until all three README translations carry the same command and claims.

- [ ] **Step 3: Update all public instructions in one change**

Apply equivalent EN/RU/ZH text. Remove the mutable/nonexistent production release URL claim already rejected by the approved design. Keep manual source checkout instructions factual and distinguish production profile from development profile.

Update `integrations/README.md` to show `uv run --locked --no-sync --directory "$LLM_WIKI_ROOT" python scripts/mcp_server.py` for POSIX shells and `uv run --locked --no-sync --directory $env:LLM_WIKI_ROOT python scripts/mcp_server.py` for PowerShell. Update `CONTRIBUTING.md` so contributors run locked dev sync and the full suite, while installed production uses no default groups.

- [ ] **Step 4: Add one Keep-a-Changelog Unreleased entry**

Under `## [Unreleased]` -> `### Fixed`, record B1-B9 in plain language: Windows stat/handle identity, explicit SQLite closure, MCP timeout-test drain, checked metadata publication, and direct locked production/optional/dev dependencies. Do not change `pyproject.toml` version and do not add a release heading.

- [ ] **Step 5: Run documentation guards**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_readme_i18n.py tests/test_quality_guards.py tests/test_integration_injection.py -q
```

Expected: PASS.

- [ ] **Step 6: Stop at a reviewable checkpoint**

Run `git diff --check`. Do not stage or commit unless explicitly requested.

### Task 13: Final B1-B9 Verification

**Files:**
- Verify all files listed above
- Do not create release artifacts

- [ ] **Step 1: Check lock and dependency contracts**

Run:

```powershell
uv --version
uv lock --check --no-python-downloads
uv run --locked --no-sync pytest tests/test_dependency_contract.py tests/test_install_smoke.py tests/test_sync_memory.py tests/test_ci_policy.py -q
```

Expected: uv 0.12.1; lock check exits 0; all contract tests PASS.

- [ ] **Step 2: Run the B1/B2 Windows identity gate**

Run on Windows under every Python 3.10-3.14 matrix entry:

```powershell
uv run --locked --no-sync pytest tests/test_markdown_transaction.py::test_prepare_and_apply_create_replace_delete tests/test_code_workspace.py::test_repository_contracts_are_frozen_slotted_normalized_and_deterministic tests/test_code_workspace.py -k "windows and identity" -q
```

Expected: PASS on each interpreter. No ctime assertion may compare a path creation time to descriptor change time, and no volume/file identifier may be truncated.

- [ ] **Step 3: Run the B7/B8/B9 focused gate**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_generation_catalog.py tests/test_search_ranking.py tests/test_evidence_graph_builder.py tests/test_archive_daily_bagit.py tests/test_generation_maintenance.py tests/test_mcp_server.py tests/test_reliable_memory.py tests/test_windows_workspace.py tests/test_memory_state_permissions.py -q
```

Expected: PASS with no leaked SQLite handles, no late MCP worker activity, and fail-closed injected durability errors.

- [ ] **Step 4: Run static and syntax gates**

Run:

```powershell
uv run --locked --no-sync ruff check scripts/ tests/ benchmark/
uv run --locked --no-sync python -m compileall -q -x "tests[\\/]fixtures[\\/]code_kernel[\\/]python[\\/]pkg[\\/]broken[.]py" scripts tests benchmark
node --check scripts/llm-wiki-memory-opencode.js
bash -n install.sh
pwsh -NoProfile -NonInteractive -Command '$tokens=$null; $errors=$null; [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path "install.ps1"), [ref]$tokens, [ref]$errors) > $null; if ($errors.Count) { $errors | Out-String | Write-Error; exit 1 }'
```

Expected: every command exits 0.

- [ ] **Step 5: Run structure and documentation gates**

Run:

```powershell
uv run --locked --no-sync pytest tests/test_structure.py tests/test_readme_i18n.py tests/test_quality_guards.py tests/test_runtime_deletion_contract.py -q
uv run --locked --no-sync python scripts/lint_memory.py --scope all --fail-on-findings --allowed-categories orphan_daily_logs missing_backlinks missing_sources_section temporal_validity
```

Expected: PASS with no new structure, runtime-root, i18n, or knowledge findings.

- [ ] **Step 6: Run the complete local Windows suite**

Run:

```powershell
uv run --locked --no-sync pytest -q
```

Expected: all collected tests pass or only explicitly documented platform skips remain. None of the original B1-B9 root-cause failures may remain.

- [ ] **Step 7: Confirm Linux and CI evidence**

Run the Linux Python 3.10/3.14 full and clean-profile jobs. Let CI provide macOS confirmation. A timeout is a failed gate; do not rerun automatically or increase a ceiling without retained duration evidence.

- [ ] **Step 8: Inspect the final diff without committing**

Run:

```powershell
git status --short
git diff --check
git diff --stat
git diff -- docs/superpowers/specs/2026-08-05-v4-reliability-repair-design.md
```

Expected: `git diff --check` exits 0; the approved design is unchanged; no runtime files, personal knowledge, release version, or unrelated worktree changes were added by implementation.

- [ ] **Step 9: Produce the closure report**

Report B1-B9 with the exact passing test or clean-environment job for each finding, platform/interpreter coverage, remaining honest skips, and any unavailable external CI evidence. State explicitly that no commit was made unless the operator requested one.

## Dependency Impact Summary

| Profile | Direct contents after this plan | Sync command | Proof |
|---|---|---|---|
| Production | MCP v1 and Python 3.10 TOML fallback | `uv sync --locked --no-default-groups` | Installer stdio smoke on 3.10 and 3.14 |
| Production rerun | Same baseline while retaining selected extras | `uv sync --locked --no-default-groups --inexact` | `tests/test_sync_memory.py` inventory/command tests |
| Hybrid | Production plus NumPy, LanceDB, sentence-transformers | `uv sync --locked --no-default-groups --extra hybrid` | Clean vector lane on 3.10 and 3.14 |
| Code graph | Production plus Jedi, tree-sitter core, 12 grammars | `uv sync --locked --no-default-groups --extra code-graph` | Clean parser lane on 3.10 and 3.14 |
| Development | Production plus pytest, Ruff, PyYAML, jsonschema, NumPy, Jedi, tree-sitter core/grammars, pre-commit | `uv sync --locked` | Full cross-platform suite |

## Completion Definition

- B1-B9 each have a regression that fails when its fix is reverted.
- Windows Python 3.10-3.14 and Linux Python 3.10/3.14 gates are green; CI records macOS confirmation.
- Production smoke succeeds without pytest or development groups and returns exactly 12 MCP tools.
- Every SQLite connection opened in the B7 inventory is explicitly closed before return.
- Durable publication never reports success after unsupported/failed flush or move and exposes old/new/duplicate evidence deterministically.
- README translations, integration docs, contributor docs, and `CHANGELOG.md` agree.
- No version bump, release, runtime path change, architecture drift, or commit occurs without a separate explicit operator request.
