# Reliable Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make automatic memory mutations recoverable, idempotent, evidence-backed, and safe across concurrent local agents and process crashes while Markdown remains authoritative.

**Architecture:** Shared restricted-canonical-JSON, hashing, path, filesystem, and rollback-journal SQLite primitives support a recoverable Markdown transaction coordinator and fenced project/queue leases. Compilation, archiving, claim evaluation, hooks, and MCP compose those primitives through durable plans, content-addressed evidence, and idempotent operation IDs; runtime databases coordinate work but never become the knowledge source of truth.

**Tech Stack:** Python 3.10+ standard library (`sqlite3`, `hashlib`, `json`, `os`, `pathlib`, `secrets`), JSON Schema documents, Markdown/OKF, MCP Python SDK, pytest, Ruff, Git.

---

## File Map

- Create `scripts/reliable_memory.py`: defaults, restricted canonical JSON, SHA-256 helpers, safe local-path checks, fsync helpers, and rollback-journal SQLite connections.
- Create `scripts/schemas/markdown-transaction-v1.json`: prepared Markdown transaction plan contract.
- Create `scripts/schemas/project-checkpoint-v1.json`: journal checkpoint event and reducer delta contract.
- Create `scripts/schemas/queue-task-v2.json`: redacted SQLite queue task contract.
- Create `scripts/schemas/compile-plan-v2.json`: normalized compiler output contract.
- Create `scripts/schemas/compile-receipt-v2.json`: durable snapshot compile receipt embedded in Markdown.
- Create `scripts/schemas/archive-manifest-v1.json`: immutable daily archive manifest contract.
- Create `scripts/schemas/claim-ledger-v1.json`, `scripts/schemas/claim-candidate-v1.json`, and `scripts/schemas/claim-relations-v1.json`: atomic claim contracts and controlled vocabulary.
- Create `scripts/markdown_transaction.py`: prepare, validate, apply, recover, undo, retention, writer-gate, and project lease coordinator.
- Create `scripts/project_journal.py`: idempotent checkpoint events, fenced sequence allocation, deterministic `state.md` projection, trigger reducer, and bounded handoff.
- Replace internals of `scripts/memory_queue.py`: SQLite queue, leases, retries, results, migration, dead-letter, redrive, export-first purge, and bounded worker.
- Modify `scripts/llm_client.py`: resolved provider/capability/call descriptors and explicit candidate-attempt API.
- Create `scripts/compile_cache.py`: compile action descriptors, source manifests, owner-only cache entries, and deterministic validation.
- Modify `scripts/compile_memory.py`: immutable source snapshots, schema-validated plans, provider-aware cache, critique descriptors, receipts, and one Markdown transaction.
- Create `scripts/evidence_resolver.py`: logical content-addressed evidence references across flat daily files and validated bags.
- Replace internals of `scripts/archive_daily.py`: BagIt-style eligibility, publication, duplicate recovery, index rebuilding, and transactional source removal.
- Create `scripts/claims.py`: claim parsing, normalization, literal evidence verification, derived SQLite index, deterministic resolution, and candidate rendering.
- Create `scripts/contradiction_pipeline.py`: evidence-conditioned evaluation, blind critique, Python lifecycle policy, and structured assessments.
- Create `benchmark/contradiction-v1.json`, `benchmark/contradiction-v1.schema.json`, and `benchmark/run_contradiction_benchmark.py`: frozen contradiction corpus, closed validation, and calibration gates.
- Modify `scripts/doctor.py` and `scripts/mcp_server.py`: safe health/recovery/queue/archive/claim surfaces with redacted structured output.
- Modify automatic writers and lifecycle adapters listed in Task 14 so all covered Markdown writes use the transaction boundary and checkpoint events.
- Modify `README.md`, `README.ru.md`, `README.zh-CN.md`, `docs/ARCHITECTURE.md`, `docs/STRUCTURE.md`, `docs/USER-GUIDE.md`, `docs/operating-model.md`, `AGENTS.md`, and `CLAUDE.md`: runtime/deletion contracts, commands, operator recovery, and identical agent contracts.

### Task 1: Shared Canonical, Filesystem, SQLite, And Schema Primitives

**Files:**
- Create: `scripts/reliable_memory.py`
- Create: `scripts/schemas/markdown-transaction-v1.json`
- Create: `scripts/schemas/project-checkpoint-v1.json`
- Create: `scripts/schemas/queue-task-v2.json`
- Create: `scripts/schemas/compile-plan-v2.json`
- Create: `scripts/schemas/compile-receipt-v2.json`
- Create: `scripts/schemas/archive-manifest-v1.json`
- Test: `tests/test_reliable_memory.py`
- Test: `tests/test_reliable_memory_schemas.py`

- [ ] **Step 1: Write failing tests for canonical encoding, defaults, SQLite mode, permissions, and unsafe state roots**

```python
def test_defaults_are_the_approved_bounded_values():
    from dataclasses import asdict
    from reliable_memory import DEFAULTS
    assert asdict(DEFAULTS) == {
        "markdown_busy_ms": 10_000, "queue_busy_ms": 5_000,
        "transaction_retention_days": 30, "artifact_retention_days": 30,
        "archive_hot_days": 90,
        "project_lease_seconds": 30, "project_heartbeat_seconds": 10,
        "checkpoint_debounce_seconds": 30, "checkpoint_fallback_events": 20,
        "queue_lease_seconds": 120, "queue_heartbeat_seconds": 40,
        "queue_max_attempts": 8, "retry_base_seconds": 30,
        "retry_cap_seconds": 3600, "worker_max_tasks": 20,
        "worker_max_seconds": 600, "worker_idle_seconds": 2,
        "priority_min": -100, "priority_max": 100,
        "queue_result_retention_days": 30, "dead_task_retention_days": None,
    }

def test_operational_connection_forbids_wal(tmp_path):
    from reliable_memory import open_operational_db
    db = open_operational_db(tmp_path / "run" / "x.sqlite3", busy_ms=10_000)
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000

def test_state_root_rejects_unc_reparse_and_failed_lock_probe(tmp_path, monkeypatch):
    from reliable_memory import UnsafeStateRoot, validate_state_root
    monkeypatch.setattr("reliable_memory._known_network_path", lambda path: True)
    with pytest.raises(UnsafeStateRoot, match="local filesystem"):
        validate_state_root(tmp_path)
```

Also assert `canonical_json_bytes()` sorts object keys, emits UTF-8 without insignificant whitespace, rejects floats/normalized-key collisions/non-string keys, and hashes equal logical objects identically. Assert created databases/cache files are owner-only where the platform supports mode bits, schemas parse as JSON, have fixed `$id`/`const` versions, reject unknown properties, and use the same operation/state/type names as the design.

- [ ] **Step 2: Run the primitive tests and verify red**

Run: `uv run pytest tests/test_reliable_memory.py tests/test_reliable_memory_schemas.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'reliable_memory'` and missing schema paths.

- [ ] **Step 3: Add the minimal shared API and committed schemas**

```python
@dataclass(frozen=True)
class ReliableMemoryDefaults:
    markdown_busy_ms: int = 10_000
    queue_busy_ms: int = 5_000
    transaction_retention_days: int = 30
    artifact_retention_days: int = 30
    archive_hot_days: int = 90
    project_lease_seconds: int = 30
    project_heartbeat_seconds: int = 10
    checkpoint_debounce_seconds: int = 30
    checkpoint_fallback_events: int = 20
    queue_lease_seconds: int = 120
    queue_heartbeat_seconds: int = 40
    queue_max_attempts: int = 8
    retry_base_seconds: int = 30
    retry_cap_seconds: int = 3600
    worker_max_tasks: int = 20
    worker_max_seconds: int = 600
    worker_idle_seconds: int = 2
    priority_min: int = -100
    priority_max: int = 100
    queue_result_retention_days: int = 30
    dead_task_retention_days: int | None = None

DEFAULTS = ReliableMemoryDefaults()

def canonical_json_bytes(value: object) -> bytes: ...
def sha256_bytes(value: bytes) -> str: ...
def validate_state_root(path: Path) -> None: ...
def open_operational_db(path: Path, *, busy_ms: int) -> sqlite3.Connection: ...
def begin_immediate(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]: ...
def fsync_file(path: Path) -> None: ...
def fsync_directory(path: Path) -> None: ...
def restricted_relative_path(value: str, allowed_roots: tuple[str, ...]) -> PurePosixPath: ...
def validate_schema(instance: object, schema_path: Path) -> None: ...
```

`open_operational_db()` must set `journal_mode=DELETE`, `synchronous=FULL`, `foreign_keys=ON`, and the supplied timeout on every connection. `validate_schema()` implements only the committed schemas' closed subset (`type`, `required`, `properties`, `additionalProperties`, `items`, `enum`, `const`, `oneOf`, scalar bounds/patterns) without adding a runtime dependency. `validate_state_root()` must reject known UNC/network mounts, Windows reparse points, and a two-connection `BEGIN IMMEDIATE` locking probe that does not produce normal SQLite contention; cloud-folder names produce a warning, not a false claim of detection. Do not add environment variables.

- [ ] **Step 4: Run primitive tests and Ruff**

Run: `uv run pytest tests/test_reliable_memory.py tests/test_reliable_memory_schemas.py -q && uv run ruff check scripts/reliable_memory.py tests/test_reliable_memory.py tests/test_reliable_memory_schemas.py`

Expected: PASS; schema assertions confirm `create|replace|delete`, `absent`, all six queue states, compile plan v2, archive manifest v1, and checkpoint v1.

- [ ] **Step 5: Commit the shared foundation**

```bash
git add docs/superpowers/plans/2026-07-13-reliable-memory.md scripts/reliable_memory.py scripts/schemas/markdown-transaction-v1.json scripts/schemas/project-checkpoint-v1.json scripts/schemas/queue-task-v2.json scripts/schemas/compile-plan-v2.json scripts/schemas/compile-receipt-v2.json scripts/schemas/archive-manifest-v1.json tests/test_reliable_memory.py tests/test_reliable_memory_schemas.py
git commit -m "feat: add reliable memory primitives and schemas"
```

### Task 2: Recoverable Markdown Transaction Preparation And Apply

**Files:**
- Create: `scripts/markdown_transaction.py`
- Create: `tests/test_markdown_transaction.py`
- Modify: `tests/test_security_invariants.py`

- [ ] **Step 1: Write failing transaction tests**

```python
def test_prepare_and_apply_create_replace_delete_atomically(vault, state_root):
    coordinator = MarkdownCoordinator(vault, state_root)
    tx = coordinator.prepare([
        MarkdownChange.create("knowledge/notes/new.md", b"new\n"),
        MarkdownChange.replace("knowledge/index.md", b"index-v2\n"),
        MarkdownChange.delete("knowledge/inbox/claims/old.md"),
    ], operation_id="compile:abc")
    assert tx.state == "prepared"
    assert coordinator.apply(tx.id).state == "committed"

def test_llm_work_cannot_run_under_writer_gate(vault, state_root):
    coordinator = MarkdownCoordinator(vault, state_root)
    with coordinator.writer_gate():
        assert coordinator.writer_gate_held()
        with pytest.raises(RuntimeError, match="writer gate"):
            coordinator.assert_external_work_allowed()
```

Cover exact `absent` semantics, duplicate operation IDs, before/after SHA-256, snapshots captured before external work, after/before images fsynced under `run/transactions/<id>/`, same-directory random replacement files, no-clobber creates, directory fsync where supported, allowed knowledge roots only, symlink/reparse/path traversal rejection, owner-restricted artifacts, schema/evidence/link validator callbacks, and `knowledge/log.md` included in the prepared image rather than appended later.

- [ ] **Step 2: Run focused tests and verify red**

Run: `uv run pytest tests/test_markdown_transaction.py tests/test_security_invariants.py -q`

Expected: FAIL because `markdown_transaction` does not exist.

- [ ] **Step 3: Implement the transaction boundary**

```python
@dataclass(frozen=True)
class MarkdownChange:
    kind: Literal["create", "replace", "delete"]
    path: str
    content: bytes | None
    @classmethod
    def create(cls, path: str, content: bytes) -> "MarkdownChange": ...
    @classmethod
    def replace(cls, path: str, content: bytes) -> "MarkdownChange": ...
    @classmethod
    def delete(cls, path: str) -> "MarkdownChange": ...

@dataclass(frozen=True)
class MarkdownOperation:
    kind: Literal["create", "replace", "delete"]
    path: str
    before_hash: str
    after_hash: str

class MarkdownCoordinator:
    def prepare(self, changes: Sequence[MarkdownChange], *, operation_id: str,
                preconditions: Mapping[str, object] | None = None,
                validators: Sequence[Validator] = ()) -> TransactionRecord: ...
    def apply(self, transaction_id: str) -> TransactionRecord: ...
    @contextmanager
    def writer_gate(self) -> Iterator[None]: ...
    def coherent_read(self, paths: Sequence[Path]) -> dict[Path, bytes | None]: ...
```

Callers provide bytes through `MarkdownChange`; the coordinator captures before-state, stages bytes, hashes them, and persists immutable `MarkdownOperation` rows. Create `run/markdown-transactions.sqlite3` tables for transactions, operations, project leases, writer ownership, and maintenance ownership. Preparation must not hold the writer gate; apply must use a short `BEGIN IMMEDIATE`, persist `applying`, recheck preconditions and every before-state, perform idempotent filesystem operations, verify after-state, then atomically persist `committed`.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_markdown_transaction.py tests/test_security_invariants.py -q`

Expected: PASS, including Windows sharing-violation simulation, POSIX directory-fsync fallback, symlink/reparse rejection, and assertions that transaction code contains no Git subprocess operation.

- [ ] **Step 5: Commit transaction preparation and apply**

```bash
git add scripts/markdown_transaction.py tests/test_markdown_transaction.py tests/test_security_invariants.py
git commit -m "feat: add recoverable markdown transactions"
```

### Task 3: Transaction Crash Recovery, Conflicts, Undo, And Retention

**Files:**
- Modify: `scripts/markdown_transaction.py`
- Create: `tests/test_markdown_transaction_recovery.py`

- [ ] **Step 1: Write boundary-kill and conflict tests**

```python
@pytest.mark.parametrize("killpoint", [
    "after_preparing", "after_images_fsynced", "after_prepared",
    "after_applying", "after_each_target", "before_commit", "after_commit",
])
def test_recovery_converges_or_preserves_unknown_bytes(killpoint, crash_worker):
    result = crash_worker(killpoint)
    recovered = result.coordinator.recover()
    assert recovered[0].state in {"committed", "discarded", "conflicted", "quarantined"}
    assert result.unknown_user_bytes_are_unchanged()

def test_undo_is_a_new_forward_transaction(coordinator, committed_tx):
    undo = coordinator.undo(committed_tx.id)
    assert undo.id != committed_tx.id
    assert undo.parent_transaction_id == committed_tx.id
    assert coordinator.apply(undo.id).state == "committed"
```

Add a second parameterized subprocess test with `after_undo_preparing`,
`after_undo_images_fsynced`, `after_undo_prepared`, `after_undo_applying`,
`after_each_undo_target`,
`before_undo_commit`, and `after_undo_commit`; recovery must converge to the
known original or committed state without overwriting unknown bytes.

Test all recovery rules: invalid `preparing` discarded without target writes; staged create conflicts if target exists; delete only proceeds from recorded hash; prepared/applying rolls forward; after-hash is idempotent; neither-hash conflicts; corrupt after-image rolls back only known hashes; unknown bytes quarantine; obsolete project token/epoch quarantine before journal/projection; after-apply external mutation is detected and not overwritten; undo rejects changed targets; retention keeps before/after bytes for 30 days; cleanup never removes nonterminal/conflicted/quarantined or undo-eligible records.

- [ ] **Step 2: Run recovery tests and verify red**

Run: `uv run pytest tests/test_markdown_transaction_recovery.py -q`

Expected: FAIL with missing `recover`, `undo`, `quarantine`, and `prune` behavior.

- [ ] **Step 3: Implement recovery and retention APIs**

```python
class MarkdownCoordinator:
    def recover(self) -> list[TransactionRecord]: ...
    def undo(self, transaction_id: str) -> TransactionRecord: ...
    def prune(self, *, retention_days: int = 30, now: datetime | None = None) -> int: ...
    def deletion_blockers(self) -> list[dict[str, str]]: ...
```

Recovery must run at the start of every `prepare()` call. Persist stable error codes (`before_hash_mismatch`, `after_image_corrupt`, `precondition_failed`, `unknown_target_bytes`) and IDs without embedding target contents in health output.

Add CLI commands `recover`, `undo <transaction-id>`, and `prune --retention-days 30`; all default to read-only reporting except the explicitly selected recovery/undo/prune action.

- [ ] **Step 4: Run recovery and transaction suites**

Run: `uv run pytest tests/test_markdown_transaction.py tests/test_markdown_transaction_recovery.py -q`

Expected: PASS at every injected process boundary and under two concurrent recovery processes.

- [ ] **Step 5: Commit recovery support**

```bash
git add scripts/markdown_transaction.py tests/test_markdown_transaction_recovery.py
git commit -m "feat: recover and undo markdown transactions"
```

### Task 4: Fenced Project Journal And Deterministic Projection

**Files:**
- Create: `scripts/project_journal.py`
- Create: `tests/test_project_journal.py`
- Modify: `knowledge/projects/_template/state.md`

- [ ] **Step 1: Write failing journal, reducer, and lease race tests**

```python
def test_checkpoint_is_append_only_idempotent_and_projects_state(project_store):
    event = checkpoint_event(occurrence_id="evt-1", idempotency_key="task:t1:done",
                             delta={"tasks": [{"id": "t1", "op": "upsert", "status": "done"}]})
    first = project_store.checkpoint("wiki", event, owner="agent-a")
    second = project_store.checkpoint("wiki", event, owner="agent-a")
    assert first.sequence == second.sequence == 1
    assert project_store.read_journal("wiki").count('"occurrence_id":"evt-1"') == 1

def test_stale_lease_owner_cannot_commit_after_new_epoch(project_store):
    old = project_store.acquire_lease("wiki", "agent-a")
    new = project_store.acquire_lease("wiki", "agent-b", now=old.expires_at)
    with pytest.raises(ProjectFenceError):
        project_store.apply_prepared_checkpoint(old.token, old.epoch)
```

Assert journal events contain occurrence/idempotency IDs, monotonic sequence, agent/session/worktree/branch/source event, trigger/reason, structured goal/phase/current task/next actions/decisions/blockers/changed files/commands/verification deltas, evidence event IDs, and last applied sequence. Test random token, monotonic epoch, 30-second lease, 10-second heartbeat, unexpired token+epoch comparisons in sequence allocation and final apply, stable IDs with upsert/close semantics, projector crash/replay, and simultaneous projectors.

- [ ] **Step 2: Run project tests and verify red**

Run: `uv run pytest tests/test_project_journal.py -q`

Expected: FAIL because `project_journal` is missing.

- [ ] **Step 3: Implement journal and projection through Markdown transactions**

```python
class ProjectStore:
    def acquire_lease(self, slug: str, owner: str, *, ttl_seconds: int = 30) -> ProjectLease: ...
    def heartbeat(self, lease: ProjectLease) -> ProjectLease: ...
    def checkpoint(self, slug: str, event: CheckpointEvent, *, owner: str) -> CheckpointReceipt: ...
    def recover(self, slug: str | None = None) -> list[CheckpointReceipt]: ...
    def render_state(self, events: Sequence[CheckpointEvent]) -> bytes: ...
```

Write compact restricted-canonical-JSON records beneath a stable Markdown header in `knowledge/projects/<slug>/journal.md`. Prepare journal append and complete `state.md` projection in one Markdown transaction whose persisted preconditions include slug, token, epoch, and lease expiry; never edit prior journal event bytes.

- [ ] **Step 4: Run project and transaction tests**

Run: `uv run pytest tests/test_project_journal.py tests/test_markdown_transaction_recovery.py -q`

Expected: PASS; stale owners cannot append events or projections after a newer epoch.

- [ ] **Step 5: Commit journal projection**

```bash
git add scripts/project_journal.py tests/test_project_journal.py knowledge/projects/_template/state.md
git commit -m "feat: project state from fenced journals"
```

### Task 5: Checkpoint Trigger Reducer And Bounded Session Handoff

**Files:**
- Modify: `scripts/project_journal.py`
- Modify: `scripts/session_start_project_state.py`
- Modify: `scripts/precompact_capture.py`
- Modify: `scripts/session_end_capture.py`
- Modify: `scripts/post_tool_capture.py`
- Modify: `scripts/event_envelope.py`
- Modify: `scripts/integration_adapter.py`
- Modify: `scripts/llm-wiki-memory-opencode.js`
- Modify: `scripts/codex_memory.py`
- Modify: `scripts/codex-memory-wrapper.ps1`
- Modify: `integrations/claude-code/settings.json`
- Create: `tests/test_project_checkpoint_triggers.py`
- Modify: `tests/test_capture_hooks.py`
- Modify: `tests/test_integration_injection.py`

- [ ] **Step 1: Write failing table-driven trigger tests**

```python
@pytest.mark.parametrize(("event", "expected"), [
    ({"type": "pre_compact"}, "before_compaction"),
    ({"type": "compaction_confirmed"}, "after_compaction"),
    ({"type": "token_usage", "percent": 60}, "token_60"),
    ({"type": "token_usage", "percent": 70}, "token_70"),
    ({"type": "token_usage", "percent": 80}, "token_forced_80"),
    ({"type": "decision"}, "decision"),
    ({"type": "blocker_opened"}, "blocker_change"),
    ({"type": "task_completed"}, "task_completed"),
    ({"type": "session_end", "dirty": True}, "session_end"),
])
def test_exact_checkpoint_triggers(reducer, event, expected):
    assert reducer.observe(event).reason == expected
```

Test correction, blocker resolution, cancellation, ownership transfer, significant failure, file/public-contract/test-status change, dirty Stop/session-idle, SessionStart recovery, dirty 10-minute and 30-minute thresholds only on the next event, no wall-clock worker, ordinary 30-second debounce, bypass classes, every twentieth significant event fallback when host token/compaction signals are absent, and non-significant reads/status messages not counted. Test handoff contains only active goal/task, at most three next actions, blockers, recent decisions, and MCP identifiers within the existing bound.

- [ ] **Step 2: Run trigger and hook tests and verify red**

Run: `uv run pytest tests/test_project_checkpoint_triggers.py tests/test_capture_hooks.py tests/test_integration_injection.py -q`

Expected: FAIL because lifecycle events do not drive `CheckpointReducer` and SessionStart does not recover the journal.

- [ ] **Step 3: Add deterministic trigger observation to thin adapters**

```python
class CheckpointReducer:
    def observe(self, event: Mapping[str, object], *, now: datetime | None = None) -> CheckpointDecision: ...

def build_handoff(project: ProjectProjection, *, max_actions: int = 3,
                  max_chars: int = 2400) -> str: ...
```

SessionStart must call transaction and project recovery before reading the projection. `integration_adapter.py` preserves trigger payload fields and observes the envelope before both delegate and direct ingestion paths, deduplicated by `event_id`. OpenCode, Codex, and Claude adapters forward only signals their hosts actually expose. Hosts that provide token estimates or compaction confirmation set explicit envelope fields; hosts that do not rely on the significant-event counter. Keep adapters non-LLM and do not add a timer, daemon, or environment setting.

- [ ] **Step 4: Run project lifecycle tests**

Run: `uv run pytest tests/test_project_journal.py tests/test_project_checkpoint_triggers.py tests/test_capture_hooks.py tests/test_integration_injection.py -q`

Expected: PASS with deterministic fake-clock assertions and no checkpoint from repeated reads.

- [ ] **Step 5: Commit lifecycle checkpoints**

```bash
git add scripts/project_journal.py scripts/session_start_project_state.py scripts/precompact_capture.py scripts/session_end_capture.py scripts/post_tool_capture.py scripts/event_envelope.py scripts/integration_adapter.py scripts/llm-wiki-memory-opencode.js scripts/codex_memory.py scripts/codex-memory-wrapper.ps1 integrations/claude-code/settings.json tests/test_project_checkpoint_triggers.py tests/test_capture_hooks.py tests/test_integration_injection.py
git commit -m "feat: checkpoint projects from lifecycle events"
```

### Task 6: SQLite Priority Queue, Leases, Retry, And Dead Letter

**Files:**
- Modify: `scripts/memory_queue.py`
- Replace: `tests/test_memory_queue.py`
- Create: `tests/test_memory_queue_races.py`

- [ ] **Step 1: Write failing queue behavior and race tests**

```python
def test_claim_orders_priority_then_availability_then_fifo(queue, clock):
    low = queue.enqueue("query", 1, {"prompt": "low"}, priority=-1)
    first = queue.enqueue("query", 1, {"prompt": "first"}, priority=10)
    second = queue.enqueue("query", 1, {"prompt": "second"}, priority=10)
    assert [queue.claim("w").id, queue.claim("w").id, queue.claim("w").id] == [first, second, low]

def test_stale_worker_cannot_publish_or_ack(queue, clock):
    task = queue.enqueue("query", 1, {"prompt": "x"})
    old = queue.claim("old", lease_seconds=120)
    clock.advance(121)
    new = queue.claim("new", lease_seconds=120)
    with pytest.raises(LeaseFenceError):
        queue.publish_result(old, operation_id=task, result=b"old")
    queue.publish_result(new, operation_id=task, result=b"new")
    queue.acknowledge(new)
```

Test canonical redacted payload/input hash, optional dedupe, task kind and handler version, states `ready|leased|blocked|succeeded|dead|cancelled`, priority range/default, 5-second busy timeout, short `BEGIN IMMEDIATE`, execution outside transactions, random lease token, expiry and heartbeat fences, stable result operation ID published before acknowledgement, at-least-once duplicate delivery, dependency blocking without attempt cost, permanent input/version errors, 8 attempts, full-jitter 30/3600 backoff, longer valid `Retry-After`, immutable attempt history, cancellation only for nonterminal tasks, dead tasks never auto-deleted, and concurrent workers claiming each row once per lease.

- [ ] **Step 2: Run queue tests and verify red**

Run: `uv run pytest tests/test_memory_queue.py tests/test_memory_queue_races.py -q`

Expected: FAIL because queue storage is file-per-task and lacks fenced SQLite states.

- [ ] **Step 3: Implement queue schema and compatibility entry points**

```python
class MemoryQueue:
    def enqueue(self, kind: str, handler_version: int, payload: Mapping[str, object], *,
                priority: int = 0, available_at: datetime | None = None,
                dedupe_key: str | None = None) -> str: ...
    def claim(self, owner: str, *, lease_seconds: int = 120) -> QueueLease | None: ...
    def heartbeat(self, lease: QueueLease, *, lease_seconds: int = 120) -> QueueLease: ...
    def publish_result(self, lease: QueueLease, *, operation_id: str, result: bytes) -> str: ...
    def acknowledge(self, lease: QueueLease) -> None: ...
    def fail(self, lease: QueueLease, failure: QueueFailure) -> None: ...
```

Keep the exact existing module-level `enqueue(task_type, payload)`, `list_pending`,
`drain_with`, and `status` signatures as callers' SQLite-backed API; the module
facade supplies handler version 1. Results live under `run/queue-results/` with
owner-only permissions and stable no-clobber publication.

- [ ] **Step 4: Run queue tests**

Run: `uv run pytest tests/test_memory_queue.py tests/test_memory_queue_races.py -q`

Expected: PASS, including deterministic jitter by injected RNG and fake clock.

- [ ] **Step 5: Commit SQLite queue core**

```bash
git add scripts/memory_queue.py tests/test_memory_queue.py tests/test_memory_queue_races.py
git commit -m "feat: move deferred work to fenced sqlite queue"
```

### Task 7: Legacy Queue Migration, Bounded Worker, Redrive, And Purge

**Files:**
- Modify: `scripts/memory_queue.py`
- Modify: `scripts/integration_adapter.py`
- Modify: `scripts/scheduled_nightly.py`
- Modify: `scripts/scheduled_weekly.py`
- Modify: `scripts/codex-memory-wrapper.ps1`
- Create: `tests/test_memory_queue_migration.py`
- Create: `tests/test_memory_queue_cli.py`
- Modify: `tests/test_audit_fixes.py`

- [ ] **Step 1: Write failing migration and operator tests**

```python
def test_migration_commits_marker_only_after_quiesced_import(queue_paths):
    queue_paths.write_legacy_json(attempts=3)
    receipt = migrate_legacy_queue(queue_paths.state_root)
    assert receipt.imported == 1
    assert (queue_paths.state_root / "run" / "queue-migrated-v2").is_file()
    assert MemoryQueue(queue_paths.state_root).get(receipt.task_ids[0]).attempts == 3

def test_live_legacy_owner_aborts_without_enabling_sqlite(queue_paths):
    queue_paths.write_live_processing_owner()
    with pytest.raises(MigrationBusy):
        migrate_legacy_queue(queue_paths.state_root)
    assert not queue_paths.marker.exists()
```

Test exclusive migration ownership, quiescing claims, `.json` and `.processing` timestamps/attempts, malformed quarantine with stable codes and no payload in output, upgraded enqueue/worker refusal to write legacy files after marker, no two writable backends, worker exits at 20 tasks/600 seconds/2 idle seconds, heartbeat every 40 seconds, redrive creates a linked new task, purge requires terminal cutoff and export path, canonical task/result manifest is hash-verified before deletion, 30-day succeeded/cancelled retention, indefinite dead retention, and retained task/result blocks `run/` deletion. Update SessionStart/nightly/Codex callers from `drain` to `work`; replace the weekly destructive `clear-failed` call with status reporting. Doctor capability repair and worker startup remain exclusively in Task 13.

- [ ] **Step 2: Run migration and CLI tests and verify red**

Run: `uv run pytest tests/test_memory_queue_migration.py tests/test_memory_queue_cli.py tests/test_audit_fixes.py tests/test_integration_injection.py -q`

Expected: FAIL with missing migration marker, `redrive`, and `purge` commands.

- [ ] **Step 3: Implement one-way migration and explicit operator commands**

```python
def migrate_legacy_queue(state_root: Path) -> MigrationReceipt: ...
def run_worker(processor: QueueProcessor, *, max_tasks: int = 20,
               max_seconds: int = 600, idle_seconds: int = 2) -> WorkerSummary: ...
def redrive(task_id: str) -> str: ...
def purge(*, terminal_before: datetime, export_path: Path) -> PurgeReceipt: ...
```

CLI choices become `list`, `status`, `work`, `cancel`, `redrive`, `migrate`, and `purge`; print IDs, counts, states, capability names, and stable error codes only.

- [ ] **Step 4: Run all queue tests**

Run: `uv run pytest tests/test_memory_queue.py tests/test_memory_queue_races.py tests/test_memory_queue_migration.py tests/test_memory_queue_cli.py -q`

Expected: PASS with no automatic dead-letter deletion and no legacy write after migration.

- [ ] **Step 5: Commit queue migration and operations**

```bash
git add scripts/memory_queue.py scripts/integration_adapter.py scripts/scheduled_nightly.py scripts/scheduled_weekly.py scripts/codex-memory-wrapper.ps1 tests/test_memory_queue_migration.py tests/test_memory_queue_cli.py tests/test_audit_fixes.py
git commit -m "feat: migrate and operate sqlite memory queue"
```

### Task 8: Provider Descriptors And Versioned Compile Action Cache

**Files:**
- Modify: `scripts/llm_client.py`
- Create: `scripts/compile_cache.py`
- Create: `tests/test_llm_descriptors.py`
- Create: `tests/test_compile_cache.py`

- [ ] **Step 1: Write failing descriptor and golden-key tests**

```python
def test_restored_preferred_provider_does_not_hit_fallback_cache(cache, providers):
    fallback = providers.descriptor("ollama", "qwen3:0.6b")
    preferred = providers.descriptor("codex", "gpt-5")
    cache.put(action_for(fallback), {"operations": []})
    assert cache.get(action_for(preferred)) is None

@pytest.mark.parametrize("dimension", [
    "compiler_version", "schema_hash", "normalization_version", "feature_flags",
    "prompt_program_hash", "provider", "model", "capabilities", "inference_settings",
    "structured_output", "fallback_lineage", "source_manifest_hash",
])
def test_every_effective_dimension_changes_action_key(dimension):
    assert action_key(changed(BASE_ACTION, dimension)) != action_key(BASE_ACTION)
```

Test sorted source tuples `(logical_path, byte_length, sha256)` for selected daily snapshots, active notes, generated knowledge snapshot, agent contract, and log tail; no absolute paths/source text in filenames; unknown/implicit model identity disables persistent hits; owner-only `cache/compile/`; payload digest; empty successful plans cache; provider/parse/critique/schema/evidence/path failures do not; deterministic validation repeats on hit; structured-output capability is explicit; no remote sharing.

- [ ] **Step 2: Run descriptor/cache tests and verify red**

Run: `uv run pytest tests/test_llm_descriptors.py tests/test_compile_cache.py -q`

Expected: FAIL because provider calls return only strings and `compile_cache` is missing.

- [ ] **Step 3: Implement explicit resolution and cache APIs**

```python
@dataclass(frozen=True)
class ProviderDescriptor:
    provider: str
    model: str | None
    capabilities: Mapping[str, object]
    inference_settings: Mapping[str, object]
    candidate_index: int
    fallback_from: tuple[str, ...]

def provider_candidates(forced: str = "") -> list[ProviderDescriptor]: ...
def call_candidate(descriptor: ProviderDescriptor, prompt: str, system_prompt: str,
                   *, max_tokens: int, schema: Mapping[str, object] | None = None) -> LLMResult: ...

class CompileCache:
    def key(self, action: CompileActionDescriptor) -> str | None: ...
    def get(self, action: CompileActionDescriptor, validator: PlanValidator) -> dict | None: ...
    def put(self, action: CompileActionDescriptor, normalized_plan: dict) -> Path: ...
```

The lookup descriptor is fully known before each candidate call: candidate 0 has
an empty `fallback_from`; after a failed attempt, candidate N records the ordered
identities and stable failure classes of candidates 0..N-1. Probe one candidate,
compute/check only that key, call it, then repeat resolution/key lookup for the
next candidate only when fallback policy permits. Stored entries include and
validate the actual attempt lineage. Preserve `call_llm()` for non-compile callers
by delegating through this API.

- [ ] **Step 4: Run provider and cache tests**

Run: `uv run pytest tests/test_llm_descriptors.py tests/test_compile_cache.py tests/test_compile_failure.py -q`

Expected: PASS; golden fixtures prove every key dimension and fallback identity.

- [ ] **Step 5: Commit descriptors and cache**

```bash
git add scripts/llm_client.py scripts/compile_cache.py tests/test_llm_descriptors.py tests/test_compile_cache.py tests/test_compile_failure.py
git commit -m "feat: cache normalized compile actions by provider"
```

### Task 9: Snapshot-Correct Transactional Compilation And Receipts

**Files:**
- Modify: `scripts/compile_memory.py`
- Modify: `scripts/rebuild_memory_index.py`
- Modify: `scripts/schemas/compile-receipt-v2.json`
- Modify: `tests/test_compile_integration.py`
- Modify: `tests/test_compile_failure.py`
- Modify: `tests/test_compile_audit.py`
- Create: `tests/test_compile_transactions.py`

- [ ] **Step 1: Write failing blocked-LLM race and all-or-nothing tests**

```python
def test_daily_append_during_llm_call_remains_pending(compiler, blocked_llm, daily):
    original = daily.read_bytes()
    run = compiler.start(daily)
    blocked_llm.wait_until_called()
    daily.write_bytes(original + b"later\n")
    blocked_llm.release(valid_empty_plan())
    receipt = run.finish()
    assert receipt.source_digest == sha256(original)
    assert compiler.select_pending() == [daily]

def test_pages_index_log_and_receipt_commit_together(compiler, crash_at):
    crash_at("after_first_target")
    compiler.compile()
    compiler.recover()
    assert compiler.tree_is_complete_before_or_complete_after()
```

Test exact snapshot bytes sent to draft/critique, schema validation before cache/application, normalization versioning, critique descriptor, actual fallback descriptor, successful empty plan caching, cache-hit revalidation, no final Markdown/timestamp/index/log caching, legacy `compiled_daily_hashes` retained diagnostically but forcing one v2 compile, receipts with source digest/action key/completion/operations/evidence integrity, no receipt on mixed/failed apply, no LLM call under writer gate, index/log included in one prepared transaction, and compile/recovery racing multiple agents.

Each successful source snapshot has a durable receipt at
`knowledge/daily/receipts/<source-sha256>.md` with OKF frontmatter and one embedded
canonical record valid against `compile-receipt-v2.json`. The receipt is a prepared
Markdown target in the same transaction as pages, lifecycle edits, index, and log.
`compiled_daily_hashes` remains a diagnostic compatibility mirror updated only
after the transaction is committed; archive eligibility trusts the receipt and
coordinator operation states, never `state.json`.

- [ ] **Step 2: Run compile transaction tests and verify red**

Run: `uv run pytest tests/test_compile_transactions.py tests/test_compile_integration.py tests/test_compile_failure.py tests/test_compile_audit.py -q`

Expected: FAIL because live files are rehashed after the model call and writes occur independently.

- [ ] **Step 3: Refactor compile into snapshot, resolve, normalize, validate, and transact**

```python
@dataclass(frozen=True)
class DailySnapshot:
    logical_path: str
    content: bytes
    sha256: str

def snapshot_compile_inputs(paths: Sequence[Path]) -> CompileInputs: ...
def resolve_compile_plan(inputs: CompileInputs, cache: CompileCache) -> ResolvedCompilePlan: ...
def prepare_compile_transaction(plan: ResolvedCompilePlan, coordinator: MarkdownCoordinator) -> str: ...
```

Generate `knowledge/index.md` bytes in-process instead of launching a writer subprocess. Remove heuristic automatic supersession from `_check_contradictions_pre_write`; Task 12 becomes the only lifecycle policy. Use provider-native structured output when supported and capability-marked prompt-only JSON otherwise.

- [ ] **Step 4: Run compile and race tests**

Run: `uv run pytest tests/test_compile_transactions.py tests/test_compile_integration.py tests/test_compile_failure.py tests/test_compile_audit.py tests/test_compile_cache.py -q`

Expected: PASS; the appended suffix remains pending and crash recovery never publishes a partial compile tree.

- [ ] **Step 5: Commit transactional compile**

```bash
git add scripts/compile_memory.py scripts/rebuild_memory_index.py scripts/schemas/compile-receipt-v2.json tests/test_compile_transactions.py tests/test_compile_integration.py tests/test_compile_failure.py tests/test_compile_audit.py
git commit -m "feat: compile immutable snapshots transactionally"
```

### Task 10: Content-Addressed Evidence Resolver And Immutable BagIt Archive

**Files:**
- Create: `scripts/evidence_resolver.py`
- Modify: `scripts/archive_daily.py`
- Modify: `scripts/compile_memory.py`
- Modify: `scripts/lint_memory.py`
- Modify: `scripts/mcp_server.py`
- Create: `tests/test_evidence_resolver.py`
- Create: `tests/test_archive_daily_bagit.py`
- Modify: `tests/test_compile_integration.py`
- Modify: `tests/test_mcp_server.py`
- Modify: `tests/test_security_invariants.py`

- [ ] **Step 1: Write failing resolver, eligibility, publication, and recovery tests**

```python
def test_flat_and_archive_resolve_identical_evidence(vault):
    ref = EvidenceRef.parse("daily:2026-01-01 sha256:" + SOURCE_HASH +
                            " block:evt-1 bytes:10-20")
    flat = EvidenceResolver(vault).resolve(ref)
    seal_daily(vault, "2026-01-01")
    archived = EvidenceResolver(vault).resolve(ref)
    assert flat.bytes == archived.bytes
    assert flat.sha256 == archived.sha256

def test_archive_never_exposes_invalid_final_bag(archiver, killpoint):
    killpoint("before_publish_rename")
    archiver.archive("2026-01-01")
    assert not archiver.final_bag_path.exists()
```

Test exact UTF-8 half-open byte/line spans and block hashes; flat-first then validated-bag lookup; ambiguity/hash mismatch fail closed; path traversal, symlink, reparse, and oversized manifest rejection. Test `bagit.txt`, `bag-info.txt`, `data/`, SHA-256 payload manifest, canonical `archive-manifest.json`, SHA-256 tag manifest; logical ID/original path/source+payload hashes/compile receipt/terminal operations/evidence spans+hashes/queue preflight/pins/retention; unique hidden sibling build, fsync, validation, atomic directory rename; sealed bag immutability; deterministic rebuildable `archive-index.json`; no gzip.

Eligibility tests must reject today, age `<=90` days, receipt mismatch, nonterminal compile operation, unresolved evidence, ready/leased/blocked/legacy queue reference, active transaction/writer, decision evidence, uncompiled content, failure, and manual pin. Duplicate recovery removes flat source only when logical ID and source hash match; mismatch quarantines without deleting either copy.

- [ ] **Step 2: Run evidence/archive tests and verify red**

Run: `uv run pytest tests/test_evidence_resolver.py tests/test_archive_daily_bagit.py -q`

Expected: FAIL because the resolver is absent and archive currently performs a direct flat move.

- [ ] **Step 3: Implement shared resolver and bag publication**

```python
@dataclass(frozen=True)
class EvidenceRef:
    daily_id: str
    source_sha256: str
    block_id: str
    byte_start: int
    byte_end: int
    @classmethod
    def parse(cls, value: str) -> "EvidenceRef": ...

class EvidenceResolver:
    def resolve(self, reference: EvidenceRef) -> ResolvedEvidence: ...

class DailyArchiver:
    def eligible(self, source: Path, *, hot_days: int = 90) -> Eligibility: ...
    def archive(self, daily_id: str) -> ArchiveReceipt: ...
    def recover(self) -> list[ArchiveReceipt]: ...
    def rebuild_index(self) -> Path: ...
```

Hidden bag construction and the final atomic directory rename are the explicit
archive-boundary exception to file-oriented Markdown transactions: the bag is an
immutable package validated as a unit, not a set of independently visible files.
After publishing and revalidating the bag, remove the flat source with a Markdown
transaction. Add source checks proving only `archive_daily.py` may use this hidden
build/final-directory-rename exception. Replace compile citation checks, lint
evidence checks, and MCP evidence reads with `EvidenceResolver.resolve()` so all
four consumers use identical flat/archive semantics. Expose `--hot-days` and
transaction-retention flags through CLI arguments, not environment variables.

- [ ] **Step 4: Run evidence/archive/security tests**

Run: `uv run pytest tests/test_evidence_resolver.py tests/test_archive_daily_bagit.py tests/test_compile_integration.py tests/test_mcp_server.py tests/test_security_invariants.py -q`

Expected: PASS, including Windows sharing/reparse simulations, POSIX symlink/fsync behavior, and crash recovery at every publication boundary.

- [ ] **Step 5: Commit evidence and archive support**

```bash
git add scripts/evidence_resolver.py scripts/archive_daily.py scripts/compile_memory.py scripts/lint_memory.py scripts/mcp_server.py tests/test_evidence_resolver.py tests/test_archive_daily_bagit.py tests/test_compile_integration.py tests/test_mcp_server.py tests/test_security_invariants.py
git commit -m "feat: archive dailies as verified immutable bags"
```

### Task 11: Atomic Claim Schemas, Parsing, Verification, And Derived Index

**Files:**
- Create: `scripts/schemas/claim-ledger-v1.json`
- Create: `scripts/schemas/claim-candidate-v1.json`
- Create: `scripts/schemas/claim-relations-v1.json`
- Create: `scripts/claims.py`
- Modify: `scripts/okf_types.py`
- Modify: `scripts/lint_memory.py`
- Create: `tests/test_claim_schemas.py`
- Create: `tests/test_claims.py`

- [ ] **Step 1: Write failing claim schema and evidence-order tests**

```python
def test_claim_cannot_reach_normalization_before_literal_evidence(claim_pipeline, bad_ref):
    with pytest.raises(EvidenceMismatch):
        block = claim_pipeline.split_blocks(source_bytes())[0]
        extracted = claim_pipeline.extract(block, raw_claim())
        claim_pipeline.verify_literal(extracted[0], bad_ref)
    assert claim_pipeline.calls == ["split_blocks", "extract", "verify_evidence"]

def test_relations_are_closed_and_values_are_typed(validate_claim):
    assert validate_claim(claim(relation="depends-on", value={"type": "entity", "value": "svc:a"}))
    assert not validate_claim(claim(relation="mentions", value={"type": "string", "value": "a"}))
```

Assert immutable ID and normalized fingerprint, text, subject, one of `equals|has-state|has-value|member-of|located-at|starts-at|ends-at|uses|depends-on`, typed string/number+unit/boolean/entity/date/timestamp value, canonical typed qualifiers, half-open validity, observed time, lifecycle, confidence, authority, exact evidence reference/hash, links, extractor/schema versions, and unknown-field rejection. Test substantive definition excludes metadata/title/summary/link/provenance/mention-like observations. Test immutable timestamp block splitting, literal UTF-8 range/hash before entailment, deterministic scalar/entity/relation normalization, SQLite claim index rebuildability, candidate retrieval, and ledgerless pages as retrieval-only context.

- [ ] **Step 2: Run claim tests and verify red**

Run: `uv run pytest tests/test_claim_schemas.py tests/test_claims.py -q`

Expected: FAIL with missing claim schema files and module.

- [ ] **Step 3: Implement claim ledger and derived index APIs**

```python
class ClaimPipeline:
    def split_blocks(self, source: bytes) -> tuple[TimestampBlock, ...]: ...
    def extract(self, block: TimestampBlock, result: Mapping[str, object]) -> tuple[Claim, ...]: ...
    def verify_literal(self, claim: Claim, reference: EvidenceRef) -> VerifiedClaim: ...
    def normalize(self, claim: VerifiedClaim) -> NormalizedClaim: ...

class ClaimIndex:
    def rebuild(self, pages: Sequence[Path]) -> None: ...
    def candidates(self, claim: NormalizedClaim, *, limit: int = 50) -> list[IndexedClaim]: ...
```

Store one restricted-canonical-JSON ledger under `## Claims` on existing OKF page types; do not add a claim page type. Register `claim-candidate` only for quarantined inbox documents, not durable note pages. Teach lint to validate both ledger and candidate schemas. Use `cache/claims.sqlite3` only as a derived index.

- [ ] **Step 4: Run claim and resolver tests**

Run: `uv run pytest tests/test_claim_schemas.py tests/test_claims.py tests/test_evidence_resolver.py -q`

Expected: PASS; no claim proceeds after unsupported or ambiguous evidence.

- [ ] **Step 5: Commit atomic claims**

```bash
git add scripts/schemas/claim-ledger-v1.json scripts/schemas/claim-candidate-v1.json scripts/schemas/claim-relations-v1.json scripts/claims.py scripts/okf_types.py scripts/lint_memory.py tests/test_claim_schemas.py tests/test_claims.py
git commit -m "feat: add evidence-verified atomic claims"
```

### Task 12: Contradiction Policy, Quarantine, Structured MCP Result, And Frozen Benchmark

**Files:**
- Create: `scripts/contradiction_pipeline.py`
- Modify: `scripts/compile_memory.py`
- Modify: `scripts/mcp_server.py`
- Create: `benchmark/contradiction-v1.json`
- Create: `benchmark/contradiction-v1.schema.json`
- Create: `benchmark/run_contradiction_benchmark.py`
- Create: `tests/test_contradiction_pipeline.py`
- Create: `tests/test_contradiction_benchmark.py`
- Modify: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing deterministic, semantic, policy, and benchmark tests**

```python
def test_semantic_conflict_is_quarantined_before_calibration(pipeline):
    result = pipeline.assess(new_claim(), candidates=[semantic_conflict()], benchmark_gate=False)
    assert result.recommendation == "quarantine"
    assert result.lifecycle_mutations == ()
    assert result.candidate_path.startswith("knowledge/inbox/claims/")

def test_page_supersedes_only_when_every_substantive_claim_is_superseded(policy):
    assert not policy.page_is_superseded([superseded_claim(), active_claim()])
    assert policy.page_is_superseded([superseded_claim(), superseded_claim()])
```

Test deterministic equality, interval, functional relation, authority, and lifecycle outcomes; deterministic supersession only for overlapping scope/time and equal-or-higher authority; `refine|supersede|keep-both|quarantine` applied only by Python; no evaluator mutation. Test evidence-conditioned first evaluation, distinct evaluator identity when available, isolated second prompt otherwise receiving evidence+label but not first rationale, disagreement/malformed/unsupported/low-confidence quarantine, inactive candidate excluded from retrieval, candidate Markdown `type: claim-candidate` and schema-valid embedded record, transaction write, no ledgerless supersession, and current string input to MCP returning structured assessments/evidence/validity/recommendations.

Freeze examples for extraction, candidate recall, contradiction class, lifecycle recommendation, false supersession, quarantine risk/coverage, and provenance correctness. The executable gate must require false supersession `<=0.01`; even after passing, semantic supersession remains disabled until an explicit future policy change.

- [ ] **Step 2: Run contradiction tests and verify red**

Run: `uv run pytest tests/test_contradiction_pipeline.py tests/test_contradiction_benchmark.py tests/test_mcp_server.py -q`

Expected: FAIL because contradiction checking is search-only and no frozen corpus exists.

- [ ] **Step 3: Implement ordered fail-closed assessment and benchmark**

```python
class ContradictionPipeline:
    def assess_raw(self, source: bytes, extraction: Mapping[str, object], *,
                   benchmark_gate: bool = False) -> tuple[ClaimAssessment, ...]: ...
    def assess(self, claim: NormalizedClaim, *,
               candidates: Sequence[IndexedClaim] | None = None,
               benchmark_gate: bool = False) -> ClaimAssessment: ...

def apply_policy(new_claim: NormalizedClaim, existing: IndexedClaim,
                 evaluations: Sequence[Evaluation]) -> LifecycleDecision: ...

def run_frozen_benchmark(corpus: Mapping[str, object]) -> BenchmarkMetrics: ...
```

`assess_raw()` is the MCP/compiler orchestration API and runs split, extract,
literal verify, and normalize before calling `assess()`. `assess()` begins at
candidate retrieval; when `candidates` is `None`, it retrieves from `ClaimIndex`,
while explicit candidates are a deterministic unit-test seam. It then runs
deterministic resolve, semantic evaluation of unresolved pairs, blind critique,
and Python policy. Candidate writes and any deterministic lifecycle edits must
share one Markdown transaction.

`contradiction-v1.schema.json` is the committed closed schema.
`contradiction-v1.json` is restricted-canonical JSON with version, relation
metadata, and cases containing case ID, new/existing schema-valid claims, literal
evidence fixtures, expected contradiction class, expected lifecycle recommendation,
and expected provenance validity. It contains at least 240 deterministic cases:
40 equality, 40 interval, 40 functional-relation, 40 authority/lifecycle, 40
keep-both/refine, and 40 quarantine; at least 200 cases are negative controls for
automatic supersession. The runner uses deterministic resolvers and the fake
provider only. Candidate recall = gold candidate retrieved / retrievable cases;
class and lifecycle F1 are macro-F1 over declared labels; false supersession =
negative controls incorrectly superseded / all negative controls; quarantine
risk/coverage report error among published cases and quarantined share;
provenance correctness = fully valid evidence/provenance outputs / all cases.
Quarantine risk = incorrectly classified published cases / all published cases,
defined as 0 when none are published; quarantine coverage = published cases / all
cases and is reported but not optimized. The gate requires extraction exact-match
F1 1.0, candidate recall 1.0, class macro-F1 1.0, lifecycle macro-F1 1.0,
provenance correctness 1.0, quarantine risk 0, and false supersession <=0.01.
Tests validate the corpus against the committed schema and require that parsing
then canonical re-encoding produces exactly the committed corpus bytes.

- [ ] **Step 4: Run contradiction benchmark and retrieval legacy gate**

Run: `uv run pytest tests/test_contradiction_pipeline.py tests/test_contradiction_benchmark.py tests/test_mcp_server.py -q && uv run python benchmark/run_contradiction_benchmark.py --corpus benchmark/contradiction-v1.json && uv run python benchmark/run_benchmark.py --legacy-only`

Expected: PASS; benchmark prints all seven metric families, false supersession at or below 1%, and the existing legacy Recall@5 gate remains 100%.

- [ ] **Step 5: Commit contradiction pipeline**

```bash
git add scripts/contradiction_pipeline.py scripts/compile_memory.py scripts/mcp_server.py benchmark/contradiction-v1.json benchmark/contradiction-v1.schema.json benchmark/run_contradiction_benchmark.py tests/test_contradiction_pipeline.py tests/test_contradiction_benchmark.py tests/test_mcp_server.py
git commit -m "feat: quarantine uncertain claim contradictions"
```

### Task 13: Doctor, MCP, Runtime Deletion Contract, And Operator Recovery

**Files:**
- Modify: `scripts/doctor.py`
- Modify: `scripts/mcp_server.py`
- Modify: `scripts/session_start_context.py`
- Modify: `tests/test_doctor.py`
- Modify: `tests/test_mcp_server.py`
- Create: `tests/test_runtime_deletion_contract.py`

- [ ] **Step 1: Write failing health, repair, and redaction tests**

```python
def test_run_deletion_reports_every_blocker(doctor_fixture):
    doctor_fixture.add_nonterminal_transaction()
    doctor_fixture.add_undo_eligible_commit()
    doctor_fixture.add_retained_queue_result()
    doctor_fixture.add_live_project_lease()
    result = run_doctor()
    assert {b["code"] for b in result["run_deletion"]["blockers"]} == {
        "transaction_nonterminal", "transaction_undo_retained",
        "queue_result_retained", "project_lease_live",
    }

def test_health_never_contains_payload_or_secret(doctor_fixture):
    doctor_fixture.enqueue_secret("Bearer secret-value")
    output = json.dumps(run_doctor())
    assert "secret-value" not in output
```

Test transaction recovery during doctor/SessionStart, transaction states/conflicts/quarantine/retention, queue states/dead/capabilities/migration/worker owner, archive duplicate/quarantine/index, claim index/schema, live writer/project lease/queue worker/maintenance owner, local filesystem/locking probe, no silent `run/` removal by repair, and deletion allowed only when every blocker is absent. Test MCP actions use the existing uniform envelope and reveal IDs/counts/states/stable codes only. The exact action enum is `status`, `queue-inspect`, `queue-cancel`, `queue-redrive`, `queue-dead-list`, `transaction-recover`, `transaction-undo`, `archive-status`, and `claim-status`. `queue-inspect|queue-cancel|queue-redrive|transaction-undo` require `target_id`; list/status actions accept `limit` in 1..100; mutation actions require `repair=true`; all other argument combinations and all unknown properties fail validation. Test `doctor --repair` acquires a fenced maintenance-owner token, heartbeats it around recovery/index/capability/worker phases, and releases it in `finally`; a concurrent repair cannot mutate or start a second worker. It recovers transactions, rebuilds derived indexes, unblocks only repaired capabilities, and starts one bounded worker.

- [ ] **Step 2: Run doctor/MCP tests and verify red**

Run: `uv run pytest tests/test_doctor.py tests/test_mcp_server.py tests/test_runtime_deletion_contract.py -q`

Expected: FAIL because Stage 2 checks and operator actions are absent.

- [ ] **Step 3: Add health checks and bounded operator actions**

```python
def _transaction_check(state_root: Path) -> dict: ...
def _queue_v2_check(state_root: Path) -> dict: ...
def _archive_check(root: Path, state_root: Path) -> dict: ...
def _run_deletion_check(state_root: Path) -> dict: ...

def _queue_action(action: str, task_id: str | None = None) -> dict: ...
def _transaction_action(action: str, transaction_id: str | None = None) -> dict: ...
```

Preserve the existing 12 MCP tool names. Define the `doctor` input as closed
`oneOf` branches for each exact action above, each with
`additionalProperties: false`, so required/forbidden combinations of `target_id`,
bounded `limit`, and `repair: true` are schema-enforced. Dispatch queue actions to `MemoryQueue`, transaction
actions to `MarkdownCoordinator`, archive status to `DailyArchiver`, and claim
status to `ClaimIndex`. Leave `repair=false` read-only. Do not expose raw queue
payloads or transaction images. SessionStart injects health only when
degraded/error and performs recovery before context. Add source-level tests proving
`install.sh`, `install.ps1`, and repair paths never delete `run/`.

- [ ] **Step 4: Run health and deletion tests**

Run: `uv run pytest tests/test_doctor.py tests/test_mcp_server.py tests/test_runtime_deletion_contract.py -q`

Expected: PASS; no repair removes operational history or starts a daemon.

- [ ] **Step 5: Commit operator recovery surfaces**

```bash
git add scripts/doctor.py scripts/mcp_server.py scripts/session_start_context.py tests/test_doctor.py tests/test_mcp_server.py tests/test_runtime_deletion_contract.py
git commit -m "feat: expose reliable memory health and recovery"
```

### Task 14: Integrate Every Automatic Markdown Writer

**Files:**
- Modify: `scripts/daily_log_append.py`
- Modify: `scripts/flush_memory.py`
- Modify: `scripts/user_prompt_capture.py`
- Modify: `scripts/tool_breadcrumb_append.py`
- Modify: `scripts/session_end_project_tag.py`
- Modify: `scripts/session_start_project_state.py`
- Modify: `scripts/feedback_capture.py`
- Modify: `scripts/bootstrap_project.py`
- Modify: `scripts/build_context.py`
- Modify: `scripts/reflection.py`
- Modify: `scripts/access_tracking.py`
- Modify: `scripts/query_memory.py`
- Modify: `scripts/archive_stale.py`
- Modify: `scripts/migrate_to_okf.py`
- Modify: `scripts/rebuild_memory_index.py`
- Modify: `scripts/blackboard.py`
- Modify: `tests/test_security_invariants.py`
- Create: `tests/test_automatic_writer_integration.py`

- [ ] **Step 1: Write failing repository-wide writer boundary tests**

```python
def test_every_script_mutating_covered_roots_uses_the_boundary():
    writers = discover_repository_writers(
        ROOT, roots=("daily", "notes", "projects", "inbox")
    )
    assert writers
    assert not (writers - approved_boundary_callers())

def test_only_archiver_has_directory_package_exception():
    assert discover_directory_renames_under_daily(SCRIPTS) == {"archive_daily.py"}
```

The repository scan covers executable `.py`, `.js`, `.ps1`, and `.sh` source under
`scripts/`, `integrations/`, and root installers, excluding tests, fixtures,
generated output, and runtime paths. Python uses AST; other languages use
conservative syntax-aware call/path patterns. It discovers calls to `open`,
`Path.write_*`, `Path.touch`, `Path.unlink`, `os.replace`, `os.rename`,
`shutil.move`, shell redirection/move/remove, and append helpers whose resolved
targets can fall under covered roots, including project JSONL and embedded journal
paths; it is not a hard-coded writer list.
Approved direct implementations are `markdown_transaction.py` target apply and
`archive_daily.py` hidden package construction/final directory publication. Add
behavioral tests for every currently discovered mutation entry point proving
concurrent daily and project JSONL appends become idempotent transactions without
interleaving, feedback/notes/project/inbox/index/log writers recover after injected
crashes, no operation invokes Git, redaction precedes preparation, unknown external
edits conflict rather than overwrite, and runtime-only writes under `cache/`,
`logs/`, and non-coordinator `run/` remain outside the boundary.

- [ ] **Step 2: Run writer integration tests and verify red**

Run: `uv run pytest tests/test_automatic_writer_integration.py tests/test_security_invariants.py tests/test_capture_hooks.py -q`

Expected: FAIL listing each remaining direct covered Markdown writer.

- [ ] **Step 3: Route covered mutations through one helper**

```python
def mutate_knowledge(operation_id: str, changes: Mapping[Path, bytes | None], *,
                     validators: Sequence[Validator] = (),
                     preconditions: Mapping[str, object] | None = None) -> TransactionRecord: ...

def append_knowledge(operation_id: str, path: Path, block: bytes) -> TransactionRecord: ...
```

`append_knowledge()` handles Markdown and project JSONL bytes. It must coherently
read and capture an expected hash under the writer gate, release the gate, then
prepare a replace/create with a stable event-derived operation ID and that expected
hash as an enforced caller precondition. The coordinator independently recaptures
the before-state and rejects preparation if it differs; callers never provide the
persisted authoritative before-hash. Apply uses the normal before-hash comparison.
On a concurrent append conflict, retry from a new coherent read with the same event
idempotency key; never overwrite the winning bytes. Preserve public function
signatures used by hook adapters. Manual `--apply` tools may share the same
boundary; they must not bypass it when writing covered paths.

- [ ] **Step 4: Run all writer and existing integration tests**

Run: `uv run pytest tests/test_automatic_writer_integration.py tests/test_security_invariants.py tests/test_capture_hooks.py tests/test_compile_integration.py tests/test_reflection.py tests/test_feedback_capture.py tests/test_access_tracking.py -q`

Expected: PASS with every automatic covered path mutation recoverable and runtime cache/log writes unaffected.

- [ ] **Step 5: Commit automatic writer integration**

```bash
git add scripts/daily_log_append.py scripts/flush_memory.py scripts/user_prompt_capture.py scripts/tool_breadcrumb_append.py scripts/session_end_project_tag.py scripts/session_start_project_state.py scripts/feedback_capture.py scripts/bootstrap_project.py scripts/build_context.py scripts/reflection.py scripts/access_tracking.py scripts/query_memory.py scripts/archive_stale.py scripts/migrate_to_okf.py scripts/rebuild_memory_index.py scripts/blackboard.py tests/test_automatic_writer_integration.py tests/test_security_invariants.py
git commit -m "refactor: transact every automatic markdown mutation"
```

### Task 15: Documentation, Contract Sync, And Final Verification

**Files:**
- Modify: `README.md`
- Modify: `README.ru.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/STRUCTURE.md`
- Modify: `docs/USER-GUIDE.md`
- Modify: `docs/operating-model.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `CHANGELOG.md`
- Modify: `CONTRIBUTING.md`
- Modify: `tests/README.md`
- Modify: `tests/test_readme_i18n.py`
- Modify: `tests/test_structure.py`
- Modify: `tests/test_quality_guards.py`

- [ ] **Step 1: Write failing documentation and structure contract tests**

```python
def test_agent_contracts_are_byte_identical(root):
    assert (root / "AGENTS.md").read_bytes() == (root / "CLAUDE.md").read_bytes()

def test_docs_name_stage_two_runtime_artifacts(root):
    structure = (root / "docs" / "STRUCTURE.md").read_text(encoding="utf-8")
    for value in ("run/markdown-transactions.sqlite3", "run/transactions/",
                  "run/queue.sqlite3", "cache/compile/", "cache/claims.sqlite3",
                  "knowledge/daily/archive/YYYY-MM/bag-"):
        assert value in structure
```

Assert all README languages agree on queue/transaction/archive commands and test count, docs state Markdown authority, rollback-journal/`synchronous=FULL`/no WAL, local-filesystem restriction, defaults and CLI overrides, temporary mixed-tree limitation for external editors, cooperating-writer CAS guarantee, 30-day undo consequence, `run/` deletion blockers, no automatic Git, no daemon/cloud/remote queue/SQLite knowledge source/exactly-once/gzip/eager backfill/semantic supersession, and archive/queue recovery procedures.

- [ ] **Step 2: Run docs tests and verify red**

Run: `uv run pytest tests/test_readme_i18n.py tests/test_structure.py tests/test_quality_guards.py -q`

Expected: FAIL because Stage 2 paths and operator contracts are not documented.

- [ ] **Step 3: Update public and agent documentation**

Document exact commands:

```bash
uv run python scripts/doctor.py
uv run python scripts/doctor.py --repair
uv run python scripts/markdown_transaction.py recover
uv run python scripts/markdown_transaction.py undo <transaction-id>
uv run python scripts/markdown_transaction.py prune --retention-days 30
uv run python scripts/memory_queue.py migrate
uv run python scripts/memory_queue.py work --max-tasks 20 --max-seconds 600 --idle-seconds 2 --lease-seconds 120 --heartbeat-seconds 40 --max-attempts 8 --retry-base-seconds 30 --retry-cap-seconds 3600
uv run python scripts/memory_queue.py redrive <task-id>
uv run python scripts/memory_queue.py purge --terminal-before <ISO-8601> --export <path>
uv run python scripts/archive_daily.py --commit --hot-days 90
uv run python benchmark/run_contradiction_benchmark.py --corpus benchmark/contradiction-v1.json
```

Keep `AGENTS.md` and `CLAUDE.md` byte-identical. Recompute the collected test
count and synchronize it in all three READMEs, `AGENTS.md`, `CLAUDE.md`,
`CONTRIBUTING.md`, `tests/README.md`, `docs/STRUCTURE.md`, `docs/USER-GUIDE.md`,
and the Unreleased section of `CHANGELOG.md`. Record Stage 2 under Unreleased.
Do not change the `pyproject.toml` version; this is not a release.

- [ ] **Step 4: Run final verification from the current branch**

Run: `git branch --show-current`

Expected: exactly `v4.0-unified-knowledge`.

Run: `uv run pytest tests/test_readme_i18n.py -q`

Expected: PASS.

Run: `uv run pytest -q`

Expected: PASS with no failures.

Run: `uv run ruff check scripts/ tests/ benchmark/`

Expected: PASS with `All checks passed!`.

Run: `uv run python scripts/lint_memory.py --scope all`

Expected: exit 0 with no structural/OKF errors.

Run: `uv run python benchmark/run_benchmark.py --legacy-only`

Expected: exit 0 with legacy Recall@5 at 100%.

Run: `uv run python benchmark/run_contradiction_benchmark.py --corpus benchmark/contradiction-v1.json`

Expected: exit 0, all frozen metrics reported, and false supersession `<=1%` while semantic lifecycle mutation remains disabled.

Run: `git diff --check && git status --short`

Expected: no whitespace errors; status lists only intended Stage 2 files and no `cache/`, `logs/`, `run/`, secrets, archive fixtures, or personal knowledge.

- [ ] **Step 5: Commit documentation and verification contracts**

```bash
git add README.md README.ru.md README.zh-CN.md docs/ARCHITECTURE.md docs/STRUCTURE.md docs/USER-GUIDE.md docs/operating-model.md AGENTS.md CLAUDE.md CHANGELOG.md CONTRIBUTING.md tests/README.md tests/test_readme_i18n.py tests/test_structure.py tests/test_quality_guards.py
git commit -m "docs: document reliable memory operations"
```

## Self-Review

- Spec coverage: Tasks 1-3 cover rollback-journal coordination, four transaction phases, crash recovery, conflicts, undo, retention, coherent reads, and cross-filesystem staging. Tasks 4-5 cover fenced journals, deterministic projection, every checkpoint trigger/default, and bounded handoff. Tasks 6-7 cover SQLite queue states, leases, retries, dead letter, migration, worker limits, redrive, retention, and export-first purge. Tasks 8-9 cover provider identity, every cache-key input, fallback selection, immutable source snapshots, schemas, receipts, and transactional compile. Tasks 10-12 cover logical evidence, BagIt publication/recovery/pins, atomic claims, ordered contradiction evaluation, quarantine, and benchmark gates. Tasks 13-15 cover doctor, MCP, deletion safety, all automatic writers, documentation, platform-specific security, and complete verification.
- Failure and race coverage: The plan injects crashes at every prepare/apply/commit/undo/archive boundary; races daily append against blocked compilation, stale project/queue leases, multiple writers/workers/projectors/recovery processes, and external edits; and tests Windows sharing/reparse plus POSIX symlink/fsync behavior.
- Defaults coverage: Every approved duration, count, priority, retention, timeout, and worker bound appears in Task 1 and is exercised through injected clocks/configuration. Runtime overrides are constructor or CLI arguments only; no environment variables are added.
- Type consistency: `MarkdownCoordinator`, `MarkdownChange`, persisted `MarkdownOperation`, `ProjectStore`, `CheckpointReducer`, `MemoryQueue`, `ProviderDescriptor`, `CompileCache`, `EvidenceRef`, `EvidenceResolver`, `ClaimPipeline`, `ClaimIndex`, and `ContradictionPipeline` retain the same names and responsibilities wherever referenced.
- Scope discipline: The plan adds no UI, service, daemon, remote cache/queue, framework dependency, graph/knowledge source database, automatic Git operation, gzip tier, eager claim backfill, or exactly-once promise.
