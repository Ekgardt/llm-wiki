# V4 Reliability Queue And Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement R1-R2, Q1-Q7, M1-M2, and L1-L6 from the approved V4 reliability design: path-bound compile authority, a canonical bounded queue, resumable corrupt-task disposition, explicit v3 database adoption, and one fenced ownership protocol for every actor that can retain `run/`.

**Architecture:** Keep Markdown authoritative and keep exactly two active operational databases. `run/queue-v3.sqlite3` owns tasks, payload bytes, capture-task links, task fences, decision indexes, corrupt export, and purge progress. `run/markdown-transactions-v3.sqlite3` owns Markdown transactions, canonical admission, project/writer projections, intent fences, and the coordinator-side projection of sealed capture bindings. An explicit offline pair cutover retains byte-identical v2 databases, installs JSON tombstones at both legacy active paths, and enables v3 mutation only after one immutable adoption record validates the complete pair.

**Tech Stack:** Python 3.10+, stdlib `sqlite3`, canonical UTF-8 JSON, rollback-journal SQLite, existing recoverable Markdown transactions, pytest, and Ruff.

---

## Execution Rules

- Implement in the prepared reliability-repair worktree.
- Do not change architecture, paths, runtime location, or the public MCP tool count beyond the approved design.
- Do not create `scripts/repair_installed_memory.py` or `tests/test_repair_installed_memory.py`. The installer plan owns that thin CLI and its CLI tests.
- This plan owns `scripts/installed_memory_repair.py`, including the two backend functions consumed by the installer plan.
- Do not rewrite or delete v2 receipts, v2 schemas, retained v2 databases, legacy readers, `compile.pid`, or `maintenance.lock`.
- Do not use `Connection.executescript()` in either operational database initializer or migration.
- Keep every SQLite connection inside an explicit `contextlib.closing(connection)` boundary. A connection context manager controls transactions; it does not close the connection.
- Follow strict TDD. Each behavior starts with a focused failing test, then the smallest implementation, then focused GREEN, then refactor while green.
- Do not commit unless the operator explicitly asks for a commit. Every checkpoint below is conditional on that separate approval.
- Do not update version, release badges, or release URLs in this slice.

## Current-Source Notes

The implementation choices below were rechecked on 2026-08-05 against current upstream documentation:

| Source | Verified implication |
|---|---|
| Python 3.14.7 `sqlite3.Connection.executescript()` | `executescript()` can issue an implicit commit under legacy transaction control. Migrations use one `execute()` per declared statement inside explicit `BEGIN IMMEDIATE` transactions. |
| Python 3.14.7 `sqlite3.Connection.backup()` | The supported backup API copies a live SQLite database to another connection. Pair adoption uses it for each v3 candidate, then closes both connections before path publication. |
| SQLite transaction documentation, updated 2026-02-18 | `BEGIN IMMEDIATE` obtains the write transaction up front. It is the mutation boundary for ownership, task transitions, and final adoption checks. |
| SQLite PRAGMA documentation | PRAGMAs can silently ignore unknown names. Every required setting is read back and compared, including integer `synchronous=2` for FULL. |
| SQLite foreign-key documentation, updated 2026-03-20 | Foreign keys are connection-local and cannot be enabled in an open transaction. Set and read back `foreign_keys=ON` before `BEGIN IMMEDIATE`; run `foreign_key_check` before candidate activation. |
| SQLite rowid and index documentation, updated 2025-05-31 and 2026-05-06 | An unaliased hidden rowid is not persistent and cannot be named as a normal index column. Resumable queue cursors and declared indexes use stable task IDs, never hidden rowids. |
| JSON Schema Draft 2020-12 validation | `maxLength` counts JSON characters, not encoded UTF-8 bytes. Schemas provide structural/character bounds; production parsers separately enforce every approved byte limit before authority is accepted. |
| Python 3.10 `json` documentation | Untrusted JSON needs an input-size bound, and default decoding accepts repeated names and non-finite constants. Queue parsing bounds bytes first and uses rejecting decode callbacks before canonical re-encoding. |

Sources:

- <https://docs.python.org/3.14/library/sqlite3.html#sqlite3.Connection.executescript>
- <https://docs.python.org/3.14/library/sqlite3.html#sqlite3.Connection.backup>
- <https://www.sqlite.org/lang_transaction.html>
- <https://www.sqlite.org/pragma.html#pragma_application_id>
- <https://www.sqlite.org/pragma.html#pragma_trusted_schema>
- <https://www.sqlite.org/foreignkeys.html>
- <https://www.sqlite.org/lang_createindex.html>
- <https://www.sqlite.org/rowidtable.html>
- <https://json-schema.org/draft/2020-12/json-schema-validation#name-maxlength>
- <https://docs.python.org/3.10/library/json.html>

Python 3.10 remains the minimum. Use `sqlite3.connect(path, isolation_level=None)` rather than the Python 3.12 `autocommit` keyword, and do not depend on `Connection.blobopen()`, which was added in Python 3.11. Payloads are capped at 1 MiB, so Python 3.10 reads one bounded BLOB and hashes it in memoryview chunks without interpreting corrupt bytes.

## Scope Ledger

| Finding | Implemented by |
|---|---|
| R1 digest-only receipts alias logical sources | Tasks 13-14: source identity, v3 receipt path, archive authority, conservative v2 handling |
| R2 compile duplicates an unbounded source blob | Task 13: bounded complete-item batches and critique over operations plus cited evidence only |
| Q1 stored hash is not enforced | Tasks 3 and 8: BLOB authority and validation at every transition |
| Q2 dedupe aliases different work | Task 8: exact `(kind, handler_version, input_hash)` comparison |
| Q3 published schema differs from export | Tasks 1 and 8: production exporter round-trip against `queue-task/v3` |
| Q4 no safe corrupt disposition | Tasks 11-12: quarantine freeze, paged lineage export, disposition-last transition, leaves-first purge |
| Q5 hostile IDs reach path components | Tasks 1 and 11: digest-only package names |
| Q6 nonterminal decisions use terminal result slot | Tasks 3 and 9: separate `semantic_decisions` ledger |
| Q7 corrupt payload cannot prove intent identity | Tasks 3 and 9: immutable links, append-only resolutions, first-consumer seal |
| M1 `executescript()` breaks assumed atomicity | Task 2: explicit statement runner and killpoint tests |
| M2 column presence accepts incomplete migration | Tasks 2-4: completed invariants and historical partial fixtures |
| L1 live actors are invisible | Tasks 6-7 and 15: canonical owner plus complete-lifetime projections |
| L2 legacy and fenced ownership race | Tasks 6-7: marker-first compatibility protocol and positive-death takeover |
| L3 pre-v3 process can restart after cutover | Task 5: legacy-path tombstones and old-client tests |
| L4 mixed adoption/deletion snapshots | Tasks 5 and 15: operation manifest, pair adoption, protected non-permit Doctor snapshot |
| L5 nested writer reacquires caller lease | Task 7: parent-referencing writer projection only |
| L6 tokenized maintenance marker breaks v2 parser | Task 7: unchanged PID-only marker bytes |

## Cross-Plan Interfaces

These interfaces are part of this plan's acceptance criteria. Do not duplicate their responsibilities in another slice.

### Platform And Durable Publication Dependency

The platform/filesystem slice owns the checked metadata publication primitives in `scripts/reliable_memory.py`. This plan consumes these exact interfaces:

```python
@dataclass(frozen=True)
class RuntimeFileIdentity:
    platform: str
    volume: str
    file_id: str
    size: int
    mtime_ns: int
```

The consumed signatures are:

- `capture_runtime_file_identity(path: Path, *, state_root: Path) -> RuntimeFileIdentity`
- `publish_runtime_file(path: Path, data: bytes, *, state_root: Path, create_only: bool, expected: RuntimeFileIdentity | None = None, expected_sha256: str | None = None, mode: int = 0o600) -> RuntimeFileIdentity`
- `sync_runtime_directory(path: Path) -> None`

The first captures POSIX device/inode or Windows `FILE_ID_INFO` without truncation. The second flushes bytes and publishes through the approved checked platform primitive. The third fails when the approved metadata-durability primitive is unavailable.

POSIX implementation requires file sync, same-filesystem link/rename, and parent-directory `fsync`. Windows implementation requires `FlushFileBuffers` plus `MoveFileExW(MOVEFILE_WRITE_THROUGH)`. This queue plan must not retain the current silent Windows `fsync_directory()` success behavior in migration, tombstone, receipt, corrupt package, or abort-receipt publication. If the platform slice is not yet merged, stop before Task 5 rather than introducing a second publisher.

### Lifecycle Capture Interface

The lifecycle/capture slice owns capture-intent and decision file bytes. This plan owns the database and fence side of the handoff and exports:

- `enqueue_capture_task(self, kind: str, handler_version: int, payload: Mapping[str, object], *, intent_id: str, intent_path: str, intent_sha256: str, capture_fence: IntentFence, owner: OwnerLease, priority: int = 0, available_at: datetime | None = None, dedupe_key: str | None = None) -> CaptureTaskBinding`; atomically insert one task and immutable capture link.
- `publish_semantic_decision(self, coordinator: MarkdownCoordinator, *, task_id: str, intent_id: str, stage: Literal["flush", "feedback", "feedback-verify"], decision_path: str, decision_sha256: str, active_link_digest: str, task_fence: TaskFence, intent_fence: IntentFence, owner: OwnerLease) -> SemanticDecision`; read back an already synced candidate file, then atomically seal and index it without touching the terminal result slot.
- `seal_capture_binding(self, task_id: str, *, consumer_kind: Literal["transaction", "terminal", "corrupt-disposition"], consumer_id: str, active_link_digest: str, task_fence: TaskFence) -> CaptureTaskBinding`; commit a non-decision first-consumer seal before any external side effect. Semantic callers must use the combined `publish_semantic_decision()` operation instead of a two-call seal/index sequence.

The capture slice computes canonical decision bytes, publishes the create-only file as candidate evidence, and calls `publish_semantic_decision()`. That method revalidates the intent fence and canonical owner, then in one queue transaction verifies the task projection/fence and active link, inserts the first-consumer seal, and inserts the decision index. A resolution that wins before that transaction leaves the candidate file as retained conflict evidence and creates neither row. No downstream provider, Markdown, terminal, or acknowledgement side effect starts until this transaction commits. The capture slice projects the sealed binding into the coordinator before a Markdown transaction. It never sets `tasks.result_reference`, `tasks.result_sha256`, or `state='succeeded'` for a decision record.

### Installer Interface

This plan creates `scripts/installed_memory_repair.py` with exactly `inspect_installed_vault(*, root: Path, state_root: Path) -> dict[str, object]` for nonmutating shared validation and `repair_installed_vault(*, root: Path, state_root: Path, adopt_ownership_v3: bool, confirm_all_agents_stopped: bool) -> dict[str, object]` for authorized resume or separately gated offline adoption.

The installer plan creates only `scripts/repair_installed_memory.py` and imports these functions. The CLI does not duplicate validators, does not import `subprocess`, and does not delete `run/`, knowledge, retired databases, tombstones, caches, or compatibility markers.

### Transaction Interface For Capture

`MarkdownCoordinator.prepare()` keeps its public shape and accepts two additional closed precondition keys:

```python
preconditions = {
    "intent_fence": {
        "intent_id": intent_id,
        "mode": "worker",
        "token": fence.token,
        "fencing_epoch": fence.epoch,
        "expires_at": timestamp(fence.expires_at),
    },
    "capture_binding": {
        "intent_id": intent_id,
        "task_id": task_id,
        "active_link_digest": binding.active_digest,
        "seal_digest": binding.seal_digest,
    },
}
```

`_check_preconditions(preconditions, database=database)` verifies both rows in the same coordinator transaction immediately before every Markdown mutation and final commit. The lifecycle slice must not read the queue and then treat that cross-database read as a transaction precondition.

## Fixed Constants

```python
QUEUE_APPLICATION_ID = 0x4C575133          # 1280790835
COORDINATOR_APPLICATION_ID = 0x4C575433    # 1280791603
OPERATIONAL_USER_VERSION = 3

QUEUE_DB_NAME = "queue-v3.sqlite3"
COORDINATOR_DB_NAME = "markdown-transactions-v3.sqlite3"
QUEUE_V2_RETIRED = "queue-v2-retired.sqlite3"
COORDINATOR_V2_RETIRED = "markdown-transactions-v2-retired.sqlite3"

MAX_QUEUE_PAYLOAD_BYTES = 1 * 1024 * 1024
MAX_QUEUE_DEPTH = 32
MAX_QUEUE_STRING_BYTES = 256 * 1024
MAX_QUEUE_CONTAINER_MEMBERS = 1024
MAX_TASK_ATTEMPTS = 100
MAX_CORRUPT_PAGE_LINKS = 1000
MAX_CORRUPT_PAGE_BYTES = 1 * 1024 * 1024
MAX_CORRUPT_PAGE_SECONDS = 5.0
MAX_TOMBSTONE_BYTES = 4 * 1024
MAX_ADOPTION_RECORD_BYTES = 64 * 1024
MAX_ABORT_RECEIPT_BYTES = 64 * 1024
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
```

Every v3 operational connection must set and read back:

```text
journal_mode = delete
synchronous = 2 (FULL)
foreign_keys = 1 (ON)
trusted_schema = 0 (OFF)
application_id = the database-specific fixed value
user_version = 3
```

`PRAGMA integrity_check` must return exactly one row containing `ok`, and `PRAGMA foreign_key_check` must return no rows before candidate activation and during explicit inspection.

## Ownership Model

Canonical admission lives only in `maintenance_owners` in the coordinator database. Domain rows are projections, not independent deletion authority.

| Role | Scope | Lease/heartbeat | Projection or compatibility evidence |
|---|---|---|---|
| `capture` | `intent:<64hex>` | 30/10 seconds | capture index and optional intent fence |
| `project` | `project:<slug>` | 30/10 seconds | `project_leases` in coordinator DB |
| `markdown-writer` | `global` | 30/10 seconds | `writer_owners` in coordinator DB |
| `queue-worker` | `worker:<actor-id>` | 120/40 seconds | `queue_ownership` in queue DB |
| `compile` | `global` | 120/40 seconds | exact three-line `compile.pid` |
| `doctor` | `doctor:<actor-id>` | 30/10 seconds | none |
| `nightly` | `global` | 120/40 seconds | ASCII decimal PID in `maintenance.lock` |
| `weekly` | `global` | 120/40 seconds | ASCII decimal PID in `maintenance.lock` |
| `lsp` | `lsp:<owner-nonce>` | 30/10 seconds | `owner.json` and `lease.json` |
| `queue-operator` | `task:<task-id>` | 120/40 seconds | `queue_ownership` in queue DB |
| `repair` | `repair:<operation-id>` | 120/40 seconds | task/intent fences as needed |
| `runtime-deletion-check` | `global` | 30/10 seconds | 20-second protected snapshot only |

Nested work uses the caller's `OwnerLease`. It validates that exact token and epoch and creates only the needed domain projection. It never reacquires or releases canonical admission. Top-level work acquires canonical admission itself.

## Database Schema

### Queue V3 Core

Create the schema one statement at a time. The following definitions are normative; whitespace is not:

```sql
CREATE TABLE tasks (
    id TEXT NOT NULL PRIMARY KEY CHECK (length(CAST(id AS BLOB)) BETWEEN 1 AND 256),
    kind TEXT NOT NULL CHECK (length(CAST(kind AS BLOB)) BETWEEN 1 AND 64),
    handler_version INTEGER NOT NULL CHECK (handler_version BETWEEN 1 AND 2147483647),
    payload_blob BLOB NOT NULL CHECK (length(payload_blob) <= 1048576),
    input_hash TEXT NOT NULL CHECK (
        length(input_hash) = 64 AND input_hash NOT GLOB '*[^0-9a-f]*'
    ),
    dedupe_key TEXT CHECK (
        dedupe_key IS NULL OR length(CAST(dedupe_key AS BLOB)) BETWEEN 1 AND 512
    ),
    state TEXT NOT NULL CHECK (state IN (
        'ready','leased','blocked','succeeded','dead','cancelled',
        'quarantine_pending','quarantined','purge_pending'
    )),
    priority INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN -100 AND 100),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 100),
    last_attempt_at TEXT,
    lease_owner TEXT CHECK (lease_owner IS NULL OR length(CAST(lease_owner AS BLOB)) <= 256),
    lease_token TEXT CHECK (lease_token IS NULL OR length(CAST(lease_token AS BLOB)) <= 256),
    lease_expires_at TEXT,
    lease_heartbeat_at TEXT,
    attempt_started_at TEXT,
    error_code TEXT CHECK (
        error_code IS NULL OR length(CAST(error_code AS BLOB)) BETWEEN 1 AND 64
    ),
    blocked_capability TEXT CHECK (
        blocked_capability IS NULL OR length(CAST(blocked_capability AS BLOB)) <= 128
    ),
    result_reference TEXT CHECK (
        result_reference IS NULL OR length(CAST(result_reference AS BLOB)) <= 4096
    ),
    result_sha256 TEXT CHECK (
        result_sha256 IS NULL OR (
            length(result_sha256) = 64 AND result_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    result_operation_id TEXT CHECK (
        result_operation_id IS NULL OR length(CAST(result_operation_id AS BLOB)) <= 4096
    ),
    redrive_of TEXT REFERENCES tasks(id),
    lineage_generation INTEGER NOT NULL DEFAULT 0 CHECK (lineage_generation >= 0)
);

CREATE UNIQUE INDEX queue_dedupe_identity ON tasks(dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX queue_claim_order ON tasks(state, priority DESC, available_at, created_at, id);
CREATE INDEX queue_redrive_parent ON tasks(redrive_of, id);

CREATE TABLE attempt_history (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    attempt INTEGER NOT NULL CHECK (attempt BETWEEN 1 AND 100),
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN (
        'succeeded','failed','blocked','cancelled','lease_expired'
    )),
    error_code TEXT CHECK (
        error_code IS NULL OR length(CAST(error_code AS BLOB)) BETWEEN 1 AND 64
    )
);

CREATE INDEX queue_attempt_history ON attempt_history(task_id, sequence);
```

Claim order is `priority DESC, available_at, created_at, id`. The final task ID tie-break is stable across restarts and `VACUUM`; hidden rowid is never persisted or named in an index.

Migrate source coordination and queue ownership to these complete path-aware shapes:

```sql
CREATE TABLE source_fences (
    logical_path TEXT NOT NULL CHECK (length(CAST(logical_path AS BLOB)) BETWEEN 1 AND 4096),
    source_digest TEXT NOT NULL CHECK (
        length(source_digest) = 64 AND source_digest NOT GLOB '*[^0-9a-f]*'
    ),
    token TEXT NOT NULL UNIQUE CHECK (length(CAST(token AS BLOB)) BETWEEN 1 AND 256),
    owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
    owner_start_identity TEXT NOT NULL CHECK (
        length(CAST(owner_start_identity AS BLOB)) BETWEEN 1 AND 512
    ),
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY(logical_path, source_digest)
);

CREATE TABLE source_failures (
    logical_path TEXT NOT NULL CHECK (length(CAST(logical_path AS BLOB)) BETWEEN 1 AND 4096),
    source_digest TEXT NOT NULL CHECK (
        length(source_digest) = 64 AND source_digest NOT GLOB '*[^0-9a-f]*'
    ),
    error_code TEXT NOT NULL CHECK (length(CAST(error_code AS BLOB)) BETWEEN 1 AND 64),
    producer TEXT NOT NULL CHECK (producer IN ('compile','queue')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(logical_path, source_digest)
);

CREATE TABLE task_source_links (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    logical_path TEXT NOT NULL CHECK (length(CAST(logical_path AS BLOB)) BETWEEN 1 AND 4096),
    source_digest TEXT NOT NULL CHECK (
        length(source_digest) = 64 AND source_digest NOT GLOB '*[^0-9a-f]*'
    ),
    PRIMARY KEY(task_id, logical_path, source_digest)
);

CREATE INDEX queue_source_tasks
    ON task_source_links(logical_path, source_digest, task_id);

CREATE TABLE queue_ownership (
    actor_id TEXT NOT NULL PRIMARY KEY CHECK (length(CAST(actor_id AS BLOB)) BETWEEN 1 AND 256),
    domain_role TEXT NOT NULL CHECK (domain_role IN ('worker','operator')),
    canonical_role TEXT NOT NULL CHECK (
        canonical_role IN (
            'queue-worker','queue-operator','repair','compile','doctor','nightly','weekly'
        )
    ),
    canonical_scope TEXT NOT NULL CHECK (
        length(CAST(canonical_scope AS BLOB)) BETWEEN 1 AND 512
    ),
    owner_token TEXT NOT NULL UNIQUE CHECK (length(CAST(owner_token AS BLOB)) BETWEEN 1 AND 256),
    fencing_epoch INTEGER NOT NULL CHECK (fencing_epoch >= 1),
    process_id INTEGER NOT NULL CHECK (process_id > 0),
    process_start_identity TEXT NOT NULL CHECK (
        length(CAST(process_start_identity AS BLOB)) BETWEEN 1 AND 512
    ),
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE(canonical_role, canonical_scope),
    CHECK (
        (domain_role = 'worker' AND canonical_role IN (
            'queue-worker','compile','doctor','nightly','weekly'
        ))
        OR
        (domain_role = 'operator' AND canonical_role IN ('queue-operator','repair'))
    )
);

CREATE TABLE task_purge_authorizations (
    task_id TEXT NOT NULL PRIMARY KEY CHECK (
        length(CAST(task_id AS BLOB)) BETWEEN 1 AND 256
    ),
    mode TEXT NOT NULL CHECK (mode IN ('ordinary','corrupt-lineage','corrupt-parent')),
    operation_id TEXT NOT NULL CHECK (
        length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 4096
    ),
    authorization_digest TEXT NOT NULL CHECK (
        length(authorization_digest) = 64
        AND authorization_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL
);
```

Queue source lookups use `(logical_path, source_digest)` joins or bound equality predicates over `source_fences`, `source_failures`, and `task_source_links`. They never substring-search a payload. Extend general enqueue with keyword-only `source_links: Sequence[tuple[str, str]] = ()`; validate normalized logical paths and lowercase digests, then insert task and links in the same transaction. Compile/deferred producers supply the complete source set; capture tasks with no daily source supply an empty sequence. During v2 reconciliation, derive links only from validated canonical structured payload fields. Retain an ambiguous source fence or task reference as a named migration blocker instead of inventing a path.

`task_purge_authorizations` is transaction-local protocol state even though it is stored in the main schema: insertion, all authorized evidence/task deletes, and authorization-row deletion occur in one `BEGIN IMMEDIATE`, so no committed authorization survives. Ordinary purge derives its operation ID and authorization digest from the already verified export manifest. Corrupt-lineage and corrupt-parent purge derive them from the exact live purge operation/token. A startup invariant rejects any surviving authorization row as `incomplete_task_purge_authorization`.

### Queue Capture And Corrupt-State Tables

```sql
CREATE TABLE capture_intents (
    intent_id TEXT NOT NULL PRIMARY KEY CHECK (
        length(intent_id) = 64 AND intent_id NOT GLOB '*[^0-9a-f]*'
    ),
    relative_path TEXT NOT NULL CHECK (length(CAST(relative_path AS BLOB)) BETWEEN 1 AND 4096),
    intent_sha256 TEXT NOT NULL CHECK (
        length(intent_sha256) = 64 AND intent_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    byte_size INTEGER NOT NULL CHECK (byte_size BETWEEN 1 AND 1048576),
    publication_state TEXT NOT NULL CHECK (publication_state IN ('pending','ready')),
    updated_at TEXT NOT NULL
);

CREATE TABLE capture_task_links (
    task_id TEXT NOT NULL PRIMARY KEY REFERENCES tasks(id),
    intent_id TEXT NOT NULL REFERENCES capture_intents(intent_id),
    intent_sha256 TEXT NOT NULL CHECK (
        length(intent_sha256) = 64 AND intent_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    handler_version INTEGER NOT NULL CHECK (handler_version BETWEEN 1 AND 2147483647),
    link_digest TEXT NOT NULL UNIQUE CHECK (
        length(link_digest) = 64 AND link_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL
);

CREATE TABLE capture_task_link_resolutions (
    resolution_digest TEXT NOT NULL PRIMARY KEY CHECK (
        length(resolution_digest) = 64 AND resolution_digest NOT GLOB '*[^0-9a-f]*'
    ),
    task_id TEXT NOT NULL REFERENCES tasks(id),
    supersedes_digest TEXT UNIQUE CHECK (
        supersedes_digest IS NULL OR (
            length(supersedes_digest) = 64
            AND supersedes_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    observed_json BLOB NOT NULL CHECK (length(observed_json) <= 65536),
    selected_intent_id TEXT REFERENCES capture_intents(intent_id),
    actor_identity TEXT NOT NULL CHECK (length(CAST(actor_identity AS BLOB)) BETWEEN 1 AND 512),
    reason TEXT NOT NULL CHECK (length(CAST(reason AS BLOB)) BETWEEN 1 AND 4096),
    created_at TEXT NOT NULL
);

CREATE TABLE capture_task_link_seals (
    task_id TEXT NOT NULL PRIMARY KEY REFERENCES tasks(id),
    active_digest TEXT NOT NULL CHECK (
        length(active_digest) = 64 AND active_digest NOT GLOB '*[^0-9a-f]*'
    ),
    consumer_kind TEXT NOT NULL CHECK (consumer_kind IN (
        'semantic-decision','transaction','terminal','corrupt-disposition'
    )),
    consumer_id TEXT NOT NULL CHECK (length(CAST(consumer_id AS BLOB)) BETWEEN 1 AND 4096),
    seal_digest TEXT NOT NULL UNIQUE CHECK (
        length(seal_digest) = 64 AND seal_digest NOT GLOB '*[^0-9a-f]*'
    ),
    sealed_at TEXT NOT NULL
);

CREATE TABLE semantic_decisions (
    intent_id TEXT NOT NULL REFERENCES capture_intents(intent_id),
    stage TEXT NOT NULL CHECK (stage IN ('flush','feedback','feedback-verify')),
    decision_path TEXT NOT NULL CHECK (length(CAST(decision_path AS BLOB)) BETWEEN 1 AND 4096),
    decision_sha256 TEXT NOT NULL CHECK (
        length(decision_sha256) = 64 AND decision_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    active_link_digest TEXT NOT NULL CHECK (
        length(active_link_digest) = 64 AND active_link_digest NOT GLOB '*[^0-9a-f]*'
    ),
    publication_state TEXT NOT NULL CHECK (publication_state = 'published'),
    published_at TEXT NOT NULL,
    PRIMARY KEY(intent_id, stage)
);

CREATE TABLE task_fence_epochs (
    task_id TEXT NOT NULL PRIMARY KEY CHECK (
        length(CAST(task_id AS BLOB)) BETWEEN 1 AND 256
    ),
    last_epoch INTEGER NOT NULL CHECK (last_epoch >= 0)
);

CREATE TABLE task_fences (
    task_id TEXT NOT NULL PRIMARY KEY REFERENCES tasks(id),
    mode TEXT NOT NULL CHECK (mode IN ('worker','queue-operator')),
    token TEXT NOT NULL UNIQUE CHECK (length(CAST(token AS BLOB)) BETWEEN 1 AND 256),
    fencing_epoch INTEGER NOT NULL CHECK (fencing_epoch >= 1),
    canonical_role TEXT NOT NULL CHECK (canonical_role IN (
        'queue-worker','compile','doctor','nightly','weekly','queue-operator','repair'
    )),
    canonical_scope TEXT NOT NULL CHECK (length(CAST(canonical_scope AS BLOB)) BETWEEN 1 AND 512),
    canonical_actor_id TEXT NOT NULL CHECK (length(CAST(canonical_actor_id AS BLOB)) BETWEEN 1 AND 256),
    canonical_owner_token TEXT NOT NULL CHECK (length(CAST(canonical_owner_token AS BLOB)) BETWEEN 1 AND 256),
    canonical_fencing_epoch INTEGER NOT NULL CHECK (canonical_fencing_epoch >= 1),
    process_id INTEGER NOT NULL CHECK (process_id > 0),
    process_start_identity TEXT NOT NULL CHECK (
        length(CAST(process_start_identity AS BLOB)) BETWEEN 1 AND 512
    ),
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    CHECK (
        (mode = 'worker' AND canonical_role IN (
            'queue-worker','compile','doctor','nightly','weekly'
        ))
        OR
        (mode = 'queue-operator' AND canonical_role IN ('queue-operator','repair'))
    )
);
```

`attempt_history`, `capture_task_links`, `capture_task_link_resolutions`, and `capture_task_link_seals` reject every update. Their delete triggers reject deletion unless the same transaction contains the matching validated `task_purge_authorizations` row. `semantic_decisions` rejects every update/delete and stores only successfully published rows; conflicting decision bytes remain lifecycle quarantine evidence and never rewrite the index row. Attempt-history insertion rejects a 101st row for one task; claim detects that bound before leasing and moves the task to `dead/attempt_history_exhausted` without dispatch. Blocked attempts may repeat an attempt ordinal because the retry counter is restored, so `(task_id, attempt)` is intentionally not unique. Resolution supersession is represented by a new row's `supersedes_digest`; no prior row is updated. The active digest is the one original link or unsuperseded resolution leaf. Zero or multiple leaves are `capture_link_conflicted`.

Declare redrive-lineage triggers for all three mutations. Inserting a row with `redrive_of`, changing either old or new `redrive_of`, or deleting a child increments every affected parent's `lineage_generation`. A `BEFORE` guard rejects insert/update/delete against a parent in `quarantine_pending`, `quarantined`, or `purge_pending`; the sole exception is deletion with a matching `corrupt-lineage` authorization while that parent has the same live purge operation. Tests assert one generation increment per changed edge, including reassignment from one parent to another, and no increment for updates that leave `redrive_of` unchanged.

Corrupt operation tables are:

```sql
CREATE TABLE corrupt_export_operations (
    operation_id TEXT NOT NULL PRIMARY KEY CHECK (
        length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 4096
    ),
    task_id TEXT NOT NULL UNIQUE CHECK (length(CAST(task_id AS BLOB)) BETWEEN 1 AND 256),
    disposition_key TEXT NOT NULL UNIQUE CHECK (
        length(disposition_key) = 64 AND disposition_key NOT GLOB '*[^0-9a-f]*'
    ),
    task_fence_token_digest TEXT NOT NULL CHECK (
        length(task_fence_token_digest) = 64
        AND task_fence_token_digest NOT GLOB '*[^0-9a-f]*'
    ),
    task_fence_epoch INTEGER NOT NULL CHECK (task_fence_epoch >= 1),
    intent_fence_digest TEXT CHECK (
        intent_fence_digest IS NULL OR (
            length(intent_fence_digest) = 64
            AND intent_fence_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    raw_sha256 TEXT NOT NULL CHECK (length(raw_sha256) = 64 AND raw_sha256 NOT GLOB '*[^0-9a-f]*'),
    history_sha256 TEXT NOT NULL CHECK (length(history_sha256) = 64 AND history_sha256 NOT GLOB '*[^0-9a-f]*'),
    metadata_sha256 TEXT NOT NULL CHECK (length(metadata_sha256) = 64 AND metadata_sha256 NOT GLOB '*[^0-9a-f]*'),
    lineage_generation INTEGER NOT NULL CHECK (lineage_generation >= 0),
    cursor_task_id TEXT NOT NULL DEFAULT '' CHECK (length(CAST(cursor_task_id AS BLOB)) <= 256),
    link_count INTEGER NOT NULL DEFAULT 0 CHECK (link_count >= 0),
    page_count INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
    rolling_root TEXT NOT NULL CHECK (length(rolling_root) = 64 AND rolling_root NOT GLOB '*[^0-9a-f]*'),
    state TEXT NOT NULL CHECK (state IN ('exporting','manifested','disposed')),
    actor_identity TEXT NOT NULL CHECK (length(CAST(actor_identity AS BLOB)) BETWEEN 1 AND 512),
    reason TEXT NOT NULL CHECK (length(CAST(reason AS BLOB)) BETWEEN 1 AND 4096),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE corrupt_export_pages (
    operation_id TEXT NOT NULL REFERENCES corrupt_export_operations(operation_id),
    page_number INTEGER NOT NULL CHECK (page_number >= 1),
    first_task_id TEXT NOT NULL CHECK (length(CAST(first_task_id AS BLOB)) BETWEEN 1 AND 256),
    last_task_id TEXT NOT NULL CHECK (length(CAST(last_task_id AS BLOB)) BETWEEN 1 AND 256),
    link_count INTEGER NOT NULL CHECK (link_count BETWEEN 1 AND 1000),
    page_sha256 TEXT NOT NULL CHECK (length(page_sha256) = 64 AND page_sha256 NOT GLOB '*[^0-9a-f]*'),
    rolling_root TEXT NOT NULL CHECK (length(rolling_root) = 64 AND rolling_root NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY(operation_id, page_number)
);

CREATE TABLE corrupt_dispositions (
    task_id TEXT NOT NULL PRIMARY KEY CHECK (length(CAST(task_id AS BLOB)) BETWEEN 1 AND 256),
    operation_id TEXT NOT NULL UNIQUE REFERENCES corrupt_export_operations(operation_id),
    package_path TEXT NOT NULL CHECK (length(CAST(package_path AS BLOB)) BETWEEN 1 AND 4096),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    disposition_sha256 TEXT NOT NULL CHECK (length(disposition_sha256) = 64 AND disposition_sha256 NOT GLOB '*[^0-9a-f]*'),
    active_link_digest TEXT CHECK (
        active_link_digest IS NULL OR (
            length(active_link_digest) = 64
            AND active_link_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    disposed_at TEXT NOT NULL
);

CREATE TABLE corrupt_package_supersession_operations (
    operation_id TEXT NOT NULL PRIMARY KEY CHECK (
        length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 4096
    ),
    package_key TEXT NOT NULL UNIQUE CHECK (
        length(package_key) = 64 AND package_key NOT GLOB '*[^0-9a-f]*'
    ),
    package_path TEXT NOT NULL CHECK (length(CAST(package_path AS BLOB)) BETWEEN 1 AND 4096),
    cursor_name TEXT NOT NULL DEFAULT '' CHECK (
        length(CAST(cursor_name AS BLOB)) <= 256
    ),
    file_count INTEGER NOT NULL DEFAULT 0 CHECK (file_count >= 0),
    page_count INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
    rolling_root TEXT NOT NULL CHECK (length(rolling_root) = 64 AND rolling_root NOT GLOB '*[^0-9a-f]*'),
    state TEXT NOT NULL CHECK (state IN ('scanning','disposed')),
    actor_identity TEXT NOT NULL CHECK (length(CAST(actor_identity AS BLOB)) BETWEEN 1 AND 512),
    reason TEXT NOT NULL CHECK (length(CAST(reason AS BLOB)) BETWEEN 1 AND 4096),
    chosen_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE corrupt_package_supersession_pages (
    operation_id TEXT NOT NULL REFERENCES corrupt_package_supersession_operations(operation_id),
    page_number INTEGER NOT NULL CHECK (page_number >= 1),
    first_name TEXT NOT NULL CHECK (
        length(CAST(first_name AS BLOB)) BETWEEN 1 AND 256
    ),
    last_name TEXT NOT NULL CHECK (
        length(CAST(last_name AS BLOB)) BETWEEN 1 AND 256
    ),
    file_count INTEGER NOT NULL CHECK (file_count BETWEEN 1 AND 1000),
    page_sha256 TEXT NOT NULL CHECK (length(page_sha256) = 64 AND page_sha256 NOT GLOB '*[^0-9a-f]*'),
    rolling_root TEXT NOT NULL CHECK (length(rolling_root) = 64 AND rolling_root NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY(operation_id, page_number)
);

CREATE TABLE corrupt_package_supersessions (
    package_key TEXT NOT NULL PRIMARY KEY CHECK (
        length(package_key) = 64 AND package_key NOT GLOB '*[^0-9a-f]*'
    ),
    operation_id TEXT NOT NULL UNIQUE REFERENCES corrupt_package_supersession_operations(operation_id),
    observed_file_count INTEGER NOT NULL CHECK (observed_file_count >= 0),
    observed_root TEXT NOT NULL CHECK (length(observed_root) = 64 AND observed_root NOT GLOB '*[^0-9a-f]*'),
    record_path TEXT NOT NULL CHECK (length(CAST(record_path AS BLOB)) BETWEEN 1 AND 4096),
    record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64 AND record_sha256 NOT GLOB '*[^0-9a-f]*'),
    disposed_at TEXT NOT NULL
);

CREATE TABLE corrupt_purge_operations (
    operation_id TEXT NOT NULL PRIMARY KEY CHECK (
        length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 4096
    ),
    task_id TEXT NOT NULL UNIQUE CHECK (length(CAST(task_id AS BLOB)) BETWEEN 1 AND 256),
    purge_token TEXT NOT NULL UNIQUE CHECK (length(CAST(purge_token AS BLOB)) BETWEEN 1 AND 256),
    expected_generation INTEGER NOT NULL CHECK (expected_generation >= 0),
    cursor_task_id TEXT NOT NULL DEFAULT '' CHECK (length(CAST(cursor_task_id AS BLOB)) <= 256),
    page_count INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
    rolling_root TEXT NOT NULL CHECK (length(rolling_root) = 64 AND rolling_root NOT GLOB '*[^0-9a-f]*'),
    state TEXT NOT NULL CHECK (state IN ('purging','receipt-published')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE corrupt_purge_pages (
    operation_id TEXT NOT NULL REFERENCES corrupt_purge_operations(operation_id),
    page_number INTEGER NOT NULL CHECK (page_number >= 1),
    first_task_id TEXT NOT NULL CHECK (length(CAST(first_task_id AS BLOB)) BETWEEN 1 AND 256),
    last_task_id TEXT NOT NULL CHECK (length(CAST(last_task_id AS BLOB)) BETWEEN 1 AND 256),
    deleted_link_count INTEGER NOT NULL CHECK (deleted_link_count BETWEEN 1 AND 1000),
    page_sha256 TEXT NOT NULL CHECK (length(page_sha256) = 64 AND page_sha256 NOT GLOB '*[^0-9a-f]*'),
    rolling_root TEXT NOT NULL CHECK (length(rolling_root) = 64 AND rolling_root NOT GLOB '*[^0-9a-f]*'),
    expected_generation INTEGER NOT NULL CHECK (expected_generation >= 0),
    PRIMARY KEY(operation_id, page_number)
);
```

Corrupt export, package-supersession, and purge operation/disposition rows intentionally retain bounded task/package identities without foreign keys to `tasks`: their rows and fixed package/receipt files outlive receipt-authorized task deletion and then follow queue-result retention. Every task operation start and resume validates the live task explicitly before mutation. Operation pages retain foreign keys to their operation row. Page and disposition rows reject every update/delete. Operation rows reject deletion and use triggers plus affected-row checks to allow only monotonic cursor/count/root updates and declared forward state transitions; arbitrary rewrites fail. Later queue-result retention is a separate explicit authority and is not implemented by task purge.

### Coordinator V3 Ownership And Fence Tables

```sql
CREATE TABLE maintenance_owner_epochs (
    role TEXT NOT NULL CHECK (role IN (
        'capture','project','markdown-writer','queue-worker','compile','doctor',
        'nightly','weekly','lsp','queue-operator','repair','runtime-deletion-check'
    )),
    scope TEXT NOT NULL CHECK (length(CAST(scope AS BLOB)) BETWEEN 1 AND 512),
    last_epoch INTEGER NOT NULL CHECK (last_epoch >= 0),
    PRIMARY KEY(role, scope)
);

CREATE TABLE maintenance_owners (
    role TEXT NOT NULL CHECK (role IN (
        'capture','project','markdown-writer','queue-worker','compile','doctor',
        'nightly','weekly','lsp','queue-operator','repair','runtime-deletion-check'
    )),
    scope TEXT NOT NULL CHECK (length(CAST(scope AS BLOB)) BETWEEN 1 AND 512),
    actor_id TEXT NOT NULL UNIQUE CHECK (length(CAST(actor_id AS BLOB)) BETWEEN 1 AND 256),
    owner_token TEXT NOT NULL UNIQUE CHECK (length(CAST(owner_token AS BLOB)) BETWEEN 1 AND 256),
    process_id INTEGER NOT NULL CHECK (process_id > 0),
    process_start_identity TEXT NOT NULL CHECK (
        length(CAST(process_start_identity AS BLOB)) BETWEEN 1 AND 512
    ),
    fencing_epoch INTEGER NOT NULL CHECK (fencing_epoch >= 1),
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    marker_path TEXT CHECK (
        marker_path IS NULL OR length(CAST(marker_path AS BLOB)) BETWEEN 1 AND 4096
    ),
    marker_sha256 TEXT CHECK (
        marker_sha256 IS NULL OR (
            length(marker_sha256) = 64 AND marker_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    marker_identity_json BLOB CHECK (
        marker_identity_json IS NULL OR length(marker_identity_json) <= 4096
    ),
    PRIMARY KEY(role, scope),
    UNIQUE(role, scope, fencing_epoch),
    UNIQUE(role, scope, actor_id, owner_token, fencing_epoch),
    CHECK (
        (
            role NOT IN ('compile','nightly','weekly')
            AND marker_path IS NULL
            AND marker_sha256 IS NULL
            AND marker_identity_json IS NULL
        )
        OR
        (
            role IN ('compile','nightly','weekly')
            AND marker_path IS NOT NULL
            AND marker_sha256 IS NOT NULL
            AND marker_identity_json IS NOT NULL
        )
    )
);

CREATE TABLE intent_fence_epochs (
    intent_id TEXT NOT NULL PRIMARY KEY CHECK (
        length(intent_id) = 64 AND intent_id NOT GLOB '*[^0-9a-f]*'
    ),
    last_epoch INTEGER NOT NULL CHECK (last_epoch >= 0)
);

CREATE TABLE intent_fences (
    intent_id TEXT NOT NULL PRIMARY KEY CHECK (
        length(intent_id) = 64 AND intent_id NOT GLOB '*[^0-9a-f]*'
    ),
    mode TEXT NOT NULL CHECK (mode IN ('capture','worker','operator')),
    token TEXT NOT NULL UNIQUE CHECK (length(CAST(token AS BLOB)) BETWEEN 1 AND 256),
    fencing_epoch INTEGER NOT NULL CHECK (fencing_epoch >= 1),
    canonical_role TEXT NOT NULL CHECK (canonical_role IN (
        'capture','queue-worker','compile','doctor','nightly','weekly',
        'queue-operator','repair'
    )),
    canonical_scope TEXT NOT NULL CHECK (length(CAST(canonical_scope AS BLOB)) BETWEEN 1 AND 512),
    canonical_actor_id TEXT NOT NULL CHECK (length(CAST(canonical_actor_id AS BLOB)) BETWEEN 1 AND 256),
    canonical_owner_token TEXT NOT NULL CHECK (length(CAST(canonical_owner_token AS BLOB)) BETWEEN 1 AND 256),
    canonical_fencing_epoch INTEGER NOT NULL CHECK (canonical_fencing_epoch >= 1),
    process_id INTEGER NOT NULL CHECK (process_id > 0),
    process_start_identity TEXT NOT NULL CHECK (
        length(CAST(process_start_identity AS BLOB)) BETWEEN 1 AND 512
    ),
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY(
        canonical_role,
        canonical_scope,
        canonical_actor_id,
        canonical_owner_token,
        canonical_fencing_epoch
    ) REFERENCES maintenance_owners(
        role,
        scope,
        actor_id,
        owner_token,
        fencing_epoch
    ),
    CHECK (
        (mode = 'capture' AND canonical_role = 'capture')
        OR
        (mode = 'worker' AND canonical_role IN (
            'queue-worker','compile','doctor','nightly','weekly'
        ))
        OR
        (mode = 'operator' AND canonical_role IN ('queue-operator','repair'))
    )
);

CREATE TABLE capture_binding_projections (
    intent_id TEXT NOT NULL PRIMARY KEY CHECK (
        length(intent_id) = 64 AND intent_id NOT GLOB '*[^0-9a-f]*'
    ),
    task_id TEXT NOT NULL UNIQUE CHECK (
        length(CAST(task_id AS BLOB)) BETWEEN 1 AND 256
    ),
    active_link_digest TEXT NOT NULL CHECK (
        length(active_link_digest) = 64 AND active_link_digest NOT GLOB '*[^0-9a-f]*'
    ),
    seal_digest TEXT NOT NULL UNIQUE CHECK (
        length(seal_digest) = 64 AND seal_digest NOT GLOB '*[^0-9a-f]*'
    ),
    projected_at TEXT NOT NULL,
    intent_fence_token TEXT NOT NULL CHECK (
        length(CAST(intent_fence_token AS BLOB)) BETWEEN 1 AND 256
    ),
    intent_fence_epoch INTEGER NOT NULL CHECK (intent_fence_epoch >= 1)
);
```

Rebuild the existing `transaction` table so its state check also accepts `aborting` and `aborted`, and add:

```sql
intent_id TEXT,
intent_fence_token TEXT,
intent_fence_epoch INTEGER,
capture_link_digest TEXT,
capture_seal_digest TEXT,
abort_operation_id TEXT UNIQUE,
abort_manifest_sha256 TEXT,
abort_receipt_sha256 TEXT,
abort_chosen_at TEXT,
aborted_at TEXT
```

`writer_owners` and `project_leases` retain their domain fields and add `canonical_role`, `canonical_scope`, `actor_id`, `process_id`, and `process_start_identity`. Direct acquisition inserts canonical and domain rows in one coordinator transaction. Nested acquisition references an already-live parent canonical row and inserts only the domain row.

## Committed JSON Contracts

Create these schemas under `scripts/schemas/` and add them to `tests/test_reliable_memory_schemas.py` without changing v2 schemas:

| Schema file | Version | Maximum encoded bytes |
|---|---|---|
| `queue-task-v3.json` | `queue-task/v3` | 64 MiB aggregate export metadata; payload itself 1 MiB |
| `compile-receipt-v3.json` | `compile-receipt/v3` | 1 MiB per receipt |
| `transaction-abort-v1.json` | `transaction-abort/v1` | 64 KiB |
| `capture-task-link-resolution-v1.json` | `capture-task-link-resolution/v1` | 64 KiB observed record |
| `corrupt-task-manifest-v1.json` | `corrupt-task-manifest/v1` | 64 KiB |
| `corrupt-task-disposition-v1.json` | `corrupt-task-disposition/v1` | 64 KiB |
| `corrupt-package-supersession-v1.json` | `corrupt-package-supersession/v1` | 64 KiB |
| `corrupt-purge-v1.json` | `corrupt-purge/v1` | 64 KiB |
| `operational-db-tombstone-v1.json` | `operational-db-tombstone/v1` | 4 KiB |
| `reliability-v3-migration-v1.json` | `reliability-v3-migration/v1` | 64 KiB |
| `reliability-v3-adoption-v1.json` | `reliability-v3-adoption/v1` | 64 KiB |

The queue export has this closed top-level shape:

```json
{
  "schema_version": "queue-task/v3",
  "task_id": "queue-task-fixture-0001",
  "kind": "compile",
  "handler_version": 1,
  "payload": {},
  "input_hash": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
  "dedupe_key": null,
  "state": "ready",
  "priority": 0,
  "created_at": "2026-08-05T12:00:00Z",
  "updated_at": "2026-08-05T12:00:00Z",
  "available_at": "2026-08-05T12:00:00Z",
  "attempts": 0,
  "last_attempt_at": null,
  "lease": null,
  "error_code": null,
  "blocked_capability": null,
  "result": null,
  "redrive_of": null,
  "lineage_generation": 0,
  "attempt_history": [],
  "source_links": [],
  "capture_binding": null
}
```

`lease` is null or exactly `{owner, token, expires_at, heartbeat_at}`. `result` is null or exactly `{reference, sha256, operation_id}`. `source_links` is sorted by `(logical_path, source_digest)`, contains closed objects with exactly those two fields, and has no duplicates. `capture_binding` is null or exactly `{intent_id, intent_sha256, handler_version, active_digest, seal_digest}`. Every schema object except the arbitrary canonical `payload` sets `additionalProperties: false`.

The compile receipt record has this closed shape:

```json
{
  "schema_version": "compile-receipt/v3",
  "source": {
    "logical_path": "knowledge/daily/2026-08-05.md",
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "byte_size": 0,
    "occurrence_bounds": null
  },
  "source_identity": "4baadbd599fef9c22e1fed700ba9896e1c7569ad2e3632e907d7ba4b5a3a5d9b",
  "batch_manifest": [
    {
      "logical_path": "knowledge/daily/2026-08-05.md",
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "byte_size": 0,
      "occurrence_bounds": null
    }
  ],
  "batch_manifest_sha256": "df4c3b125717ea5cd3e0d9b2bd3dd5cb49744126650caef2ad829146e374f65f",
  "action_key": "e9d6c77149458cf6c916ed40b7ec92986dad7e745598f456ef714bf7896679d8",
  "operation_id": "compile:76a324285f5b1833cda7953a7045d4a57cba98eb6ff4714ba2f78405472a65b8",
  "packing": {
    "algorithm": "compile-complete-items/v1",
    "tokenizer_identity": "utf8-byte-estimate/v1",
    "count_source": "estimated",
    "max_input_tokens": 32768,
    "reserved_output_tokens": 4000,
    "safety_margin_tokens": 1024,
    "measured_input_tokens": 12000
  },
  "provider_budget": {
    "provider": "fake",
    "model": "fake-v1",
    "max_output_tokens": 4000
  },
  "dispositions": [
    {
      "source_identity": "4baadbd599fef9c22e1fed700ba9896e1c7569ad2e3632e907d7ba4b5a3a5d9b",
      "disposition": "no_durable_content"
    }
  ],
  "operations": [],
  "evidence": []
}
```

The fixture uses an empty source, so its source digest is `EMPTY_SHA256`. Its source identity hashes canonical `[logical_path, sha256]`; its manifest digest hashes the exact `batch_manifest` array; its action key hashes canonical `{"fixture":"compile-receipt-v3"}`; and its operation suffix hashes canonical `{action_key, batch_manifest_sha256, dispositions}`. Tests recompute all four values rather than treating the displayed digests as unrelated examples.

`batch_manifest` is sorted by logical path and contains complete source descriptors. `dispositions` has exactly one sorted `{source_identity, disposition}` entry per manifest member, where disposition is only `compiled` or `no_durable_content`. The receipt's source must be one manifest member. `operations` is the same complete path-sorted operation list in every receipt for the batch; `evidence` is the source-sorted subset whose source identity equals that receipt's source and whose operation path exists in `operations`. Receipt bytes contain no wall-clock completion time, so a retry of the same successful batch produces the same path and bytes. Commit time remains transaction metadata.

The tombstone shape is:

```json
{
  "schema_version": "operational-db-tombstone/v1",
  "database": "queue",
  "source_state": "upgrade",
  "legacy_path": "run/queue.sqlite3",
  "replacement_path": "run/queue-v3.sqlite3",
  "retired_path": "run/queue-v2-retired.sqlite3",
  "retired_sha256": "fdc558565db7af20b2575b742eec196aa849376d3187f9dbdc0154e62f6c6999",
  "operation_id": "reliability-v3:54b59979e2b8a1202629857747bb714d840aab8a77208da637fc6bb81b745751",
  "adoption_schema_sha256": "02df576bf6a53c1a38c464b0515130924fdb3ae974df6802b7cacb9fdc2e7cad"
}
```

For `source_state="fresh"`, `retired_path` and `retired_sha256` are absent, not null. The same rule applies to the coordinator tombstone.

The package-supersession record has this closed shape and does not enumerate every page:

```json
{
  "schema_version": "corrupt-package-supersession/v1",
  "package_key": "42fbe6ca5d6056b0ff192b9029e89df6bfa9fb758a3c9e2a142d1392bc35fdde",
  "package_path": "run/queue-results/corrupt-42fbe6ca5d6056b0ff192b9029e89df6bfa9fb758a3c9e2a142d1392bc35fdde",
  "initial_identity": {
    "platform": "posix",
    "volume": "dev:2049",
    "file_id": "ino:1048576",
    "size": 4096,
    "mtime_ns": 1785931200000000000
  },
  "observed_file_count": 2503,
  "page_count": 3,
  "observed_root": "06dedfd36393cf68f9bb0d6588998569561f90105158c5b7c75e9e1361fdfd02",
  "actor_identity": "posix-uid:1000",
  "reason": "Retain and supersede the conflicting package fixture.",
  "disposed_at": "2026-08-05T12:00:00Z"
}
```

The final root commits the ordered page chain; the database page rows provide resumable detail. A verified empty package has `observed_file_count=0`, `page_count=0`, and `observed_root=EMPTY_SHA256`. `disposed_at` is copied from immutable operation `chosen_at`, so retry publishes identical bytes. Production validation rechecks all string byte limits and exact package identity before accepting this record.

## Crash Matrices

### Pair Adoption

| Last durable boundary | Required restart behavior |
|---|---|
| No migration manifest | Inspect only. Upgrade requires explicit offline adoption; a provably empty fresh vault may start fresh creation. |
| Manifest only | Revalidate exact source paths, identities, hashes, schemas, and integration digest; resume only an exact match. |
| Retired temporary copy | Verify byte equality and publish create-only to fixed retired path. |
| Retired path published | Keep it forever in this repair; continue candidate backup. |
| Candidate backup incomplete | Delete only the exact operation-keyed candidate after validation proves it is not active, then restart backup. |
| Candidate complete | Validate integrity, foreign keys, schema reconciliation, PRAGMAs, application ID, and user version. |
| Prepared tombstone only | Verify exact prepared bytes and source identity; resume replacement. |
| Legacy path replaced by tombstone | Old client is blocked. Runtime mutation remains disabled. Publish the validated candidate. |
| One v3 active path published | Keep vault offline, validate the first side, and resume the second side under the same operation ID. |
| Both v3 paths published | Revalidate both, both tombstones, retained hashes, and integration digest, then create adoption record. |
| Adoption record published | Normal v3 startup validates the whole set before every mutating open. |
| Any mismatched existing artifact | Report `reliability_v3_adoption_conflict`; never overwrite, delete, or guess. |

### Transaction Abort

| State | Receipt state | Required recovery |
|---|---|---|
| `preparing`/`prepared`/`applying` | absent | Under operator intent fence, choose forward commit if commit authority already exists; otherwise atomically enter `aborting`. |
| `aborting` | absent | Rollback-only. Restore and verify each before image, then publish receipt. |
| `aborting` | valid exact receipt | Recheck fence and tree in one coordinator transaction, then adopt `aborted`. |
| `aborting` | conflicting receipt | Quarantine transaction; do not publish terminal discard. |
| `aborted` | valid exact receipt | Terminal rollback authority; discard may bind receipt digest. |
| `aborted` | missing or conflicting receipt | Corruption blocker. |
| `committed` | any | Discard forbidden; recovery must publish `markdown_committed`. |
| `conflicted`/`quarantined` | any | Abort forbidden; explicit transaction repair remains required. |

### Corrupt Export And Purge

| Last durable boundary | Required restart behavior |
|---|---|
| Task marked `dead/payload_hash_mismatch` | Never execute, redrive, auto-export, or purge. |
| Export operation inserted and task `quarantine_pending` | Adopt exact operation and continue fixed-file publication. |
| Raw/history/metadata files published | Verify hashes; continue paged incoming-lineage export. |
| Some lineage pages published | Adopt exact pages, rolling root, and cursor; emit at most one bounded page per invocation. |
| Manifest published | Final transaction re-streams payload/history, verifies metadata plus frozen generation/count, then publishes disposition row and `quarantined` state last. |
| Orphan exact package | Adopt it into the matching operation. |
| Orphan nonmatching package | Retained blocker; only explicit superseded-package disposition is allowed. |
| Supersession scan/page interrupted | Verify exact fixed-name page, cursor, count, directory identity, and rolling root; resume one bounded page. |
| Supersession disposition published without row | Re-stream the package, verify exact record bytes/root, and adopt the immutable row; any mismatch remains blocked. |
| Purge operation inserted and parent `purge_pending` | Resume leaves-first child deletion using exact purge token. |
| Some purge pages committed | Verify generation and page receipts, then continue. |
| Purge receipt published with row present | Final transaction revalidates zero incoming links and exact authority, then deletes history/task. |
| Parent deleted without receipt | Impossible through API; Doctor reports corruption if observed. |

## File Map

| Path | Responsibility |
|---|---|
| `scripts/reliable_memory.py` | Explicit-transaction operational DB contract, PRAGMA readback, migration statement runner, checked publication dependency |
| `scripts/operational_ownership.py` | Process-start identity, positive liveness proof, canonical owner registry, lease heartbeat/release, marker identity |
| `scripts/installed_memory_repair.py` | Read-only inspection, fresh pair initialization, explicit offline adoption, partial-cutover resume, shared repair backend |
| `scripts/memory_queue.py` | V3 queue schema and transitions, BLOB authority, dedupe, task fences, links, decisions, corrupt export/purge, queue projections and CLI |
| `scripts/markdown_transaction.py` | V3 coordinator schema, writer/project projections, intent fences, capture-binding SQL preconditions, abort receipts and rollback-only recovery |
| `scripts/compile_cache.py` | Source occurrence descriptor and packing/provider identity used by action keys and receipts |
| `scripts/compile_memory.py` | Bounded batches, v3 receipt parse/read/write, top-level or nested compile ownership |
| `scripts/archive_daily.py` | Logical-path plus digest receipt authority; v2 remains non-authoritative history |
| `scripts/project_journal.py` | Canonical project admission plus same-transaction project projection |
| `scripts/lsp_process.py`, `scripts/install_pyright.py` | Canonical LSP admission and shared process-start identity; filesystem projection first/last ordering |
| `scripts/maybe_compile.py` | Legacy three-line marker compatibility without independent ownership |
| `scripts/scheduled_nightly.py`, `scripts/scheduled_weekly.py` | Complete-run canonical lease, unchanged PID marker, nested owner propagation, terminal child outcomes |
| `scripts/mcp_server.py` | Pass an existing owner into in-process compile instead of starting an unowned mutation |
| `scripts/doctor.py` | V3 validators, orphan projection blockers, protected quiescent snapshot, no durable deletion permit |
| `scripts/schemas/*.json` listed above | Committed v3 and recovery contracts |
| `tests/test_operational_migrations.py` | Statement killpoints, partial historical schemas, completed invariants, PRAGMA contracts |
| `tests/test_reliability_v3_adoption.py` | Fresh/upgrade pair cutover, tombstones, retained bytes, old clients, every publication crash boundary |
| `tests/test_operational_ownership.py` | All roles, process-start identity, takeover, admission races, marker compatibility, projection ordering |
| `tests/test_queue_v3_corruption.py` | Every payload transition, quarantine export pages, disposition CAS, hostile IDs, purge resume |
| `tests/test_queue_v3_capture_links.py` | Link agreement, resolution chain, first seal, decision ledger, task/intent fence ordering |
| `tests/test_transaction_abort.py` | `aborting`/`aborted`, receipt publication, rollback killpoints, discard preconditions |
| Existing queue, compile, archive, transaction, Doctor, project, LSP, scheduler, structure, and schema tests | Regression and integration coverage updated to v3 paths and semantics |

### Task 1: Freeze The V3 JSON Contracts

**Files:**
- Create: `scripts/schemas/queue-task-v3.json`
- Create: `scripts/schemas/compile-receipt-v3.json`
- Create: `scripts/schemas/transaction-abort-v1.json`
- Create: `scripts/schemas/capture-task-link-resolution-v1.json`
- Create: `scripts/schemas/corrupt-task-manifest-v1.json`
- Create: `scripts/schemas/corrupt-task-disposition-v1.json`
- Create: `scripts/schemas/corrupt-package-supersession-v1.json`
- Create: `scripts/schemas/corrupt-purge-v1.json`
- Create: `scripts/schemas/operational-db-tombstone-v1.json`
- Create: `scripts/schemas/reliability-v3-migration-v1.json`
- Create: `scripts/schemas/reliability-v3-adoption-v1.json`
- Modify: `tests/test_reliable_memory_schemas.py`

- [ ] **Step 1: Write failing schema registry and closed-shape tests**

Add all eleven `(file, $id, schema_version)` tuples to `SCHEMAS`. Add these behavioral tests:

- `test_queue_v3_schema_accepts_the_complete_contract_fixture`: validate the complete normative fixture above, then prove removing or adding each closed top-level field fails validation. The production-exporter round trip remains Task 8's Q3 regression because the v3 exporter does not exist in Task 1.
- `test_queue_v3_schema_has_exactly_nine_closed_states`: compare the schema enum directly with the ordered state list below and reject a tenth value.
- `test_compile_receipt_v3_requires_path_bound_source_and_all_dispositions`: validate a two-source manifest, then reject a missing logical path, duplicate source identity, missing disposition, extra disposition, and unknown disposition.
- `test_fresh_tombstone_forbids_retired_fields`: validate the fresh branch and reject either retired field separately.
- `test_upgrade_tombstone_requires_retired_path_and_hash`: validate the upgrade branch and reject either missing retired field separately.
- `test_abort_and_corrupt_receipts_reject_unknown_fields`: parameterize every new recovery schema and add one unknown key at each closed object level.

Use the normative records above. Assert queue states in this exact order:

```python
[
    "ready", "leased", "blocked", "succeeded", "dead", "cancelled",
    "quarantine_pending", "quarantined", "purge_pending",
]
```

- [ ] **Step 2: Run the schema tests and verify RED**

Run:

```bash
uv run --locked --no-sync pytest tests/test_reliable_memory_schemas.py -q
```

Expected: FAIL because the eleven committed schemas do not exist.

- [ ] **Step 3: Add the complete closed schemas**

Use Draft 2020-12, `https://llm-wiki.local/schemas/<filename>` IDs, the versions and structural/character bounds in this plan, and `additionalProperties: false` on every authority object. Use `oneOf` for fresh versus upgrade tombstones. Because `maxLength` is character-counted, add multibyte just-under/just-over regressions against each production parser and do not claim the schema alone enforces UTF-8 byte limits. Keep v2 schema files byte-identical.

- [ ] **Step 4: Run focused schema tests GREEN**

Run:

```bash
uv run --locked --no-sync pytest tests/test_reliable_memory_schemas.py -q
uv run --locked --no-sync ruff check tests/test_reliable_memory_schemas.py
```

Expected: PASS; v2 and v3 contracts coexist.

- [ ] **Step 5: Optional checkpoint after explicit operator approval**

```bash
git add scripts/schemas tests/test_reliable_memory_schemas.py
git commit -m "test: freeze reliability v3 schemas"
```

### Task 2: Add Explicit Operational Migrations And PRAGMA Contracts

**Files:**
- Modify: `scripts/reliable_memory.py`
- Create: `tests/test_operational_migrations.py`
- Modify: `tests/test_reliable_memory.py`

- [ ] **Step 1: Write failing connection and statement-runner tests**

Cover these exact behaviors:

- `test_v3_connection_reads_back_delete_full_foreign_keys_and_untrusted_schema`: open a candidate and assert `delete`, `2`, `1`, and `0` from PRAGMA readback.
- `test_v3_connection_rejects_wrong_application_id_or_user_version`: alter each header value independently and assert the stable contract-mismatch code.
- `test_each_migration_statement_has_before_after_execute_and_after_commit_killpoints`: compare emitted `before:<name>`, `after_execute:<name>`, and `after_commit:<name>` events with every declared statement name.
- `test_restart_skips_only_a_statement_with_its_completed_invariant`: crash after one statement, rerun, and assert only that completed statement is skipped.
- `test_column_presence_without_backfill_index_or_marker_is_incomplete`: build one fixture per missing invariant and assert migration repairs it or reports the named ambiguity blocker.
- `test_operational_initializers_never_call_executescript`: monkeypatch the module's `sqlite3.connect` call to use a `sqlite3.Connection` subclass whose `executescript` method raises, then initialize both candidates successfully and assert the trap was never called.
- `test_every_connection_is_closed_before_replace_or_unlink_on_windows`: wrap the connection factory with a close tracker and make publication fail if any source or target handle remains open.

Parameterize killpoints over every statement name. Launch a subprocess that exits 86 at `before:<name>`, `after_execute:<name>` before commit, and `after_commit:<name>`, rerun migration, and assert the full invariant rather than only table/column presence. The pre-commit crash must prove the statement rolled back; the post-commit crash must prove the completed invariant makes replay skip exactly that statement.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run --locked --no-sync pytest tests/test_operational_migrations.py tests/test_reliable_memory.py -q
```

Expected: FAIL because the v3 contract and resumable statement API are absent.

- [ ] **Step 3: Implement the exact migration API**

Add:

```python
@dataclass(frozen=True)
class OperationalDatabaseContract:
    application_id: int
    user_version: int = 3


@dataclass(frozen=True)
class MigrationStatement:
    name: str
    sql: str
    completed: Callable[[sqlite3.Connection], bool] = field(compare=False, repr=False)
    parameters: Sequence[object] = ()
```

The public `open_operational_db` signature is `open_operational_db(path: Path, *, busy_ms: int, contract: OperationalDatabaseContract | None = None, initialize_contract: bool = False) -> sqlite3.Connection`. Its body uses `sqlite3.connect(path, isolation_level=None)`, applies and reads back every connection PRAGMA, validates active database IDs, and returns the still-open connection to a caller-owned `contextlib.closing` block.

The migration runner signature is `run_resumable_migration(connection: sqlite3.Connection, statements: Sequence[MigrationStatement], *, final_invariant: Callable[[sqlite3.Connection], bool], killpoint: Callable[[str], None] | None = None) -> None`.

Open with `isolation_level=None`. Set connection PRAGMAs outside a transaction and read them back. For each incomplete statement, invoke `before:<name>`, enter `begin_immediate()`, execute exactly one declared SQL statement, verify that statement's completed invariant inside the same transaction, invoke `after_execute:<name>` while rollback is still possible, commit by leaving the context, then invoke `after_commit:<name>`. Finally verify the complete schema every startup. `initialize_contract=True` is legal only for an unpublished candidate and sets application/user IDs after full schema validation; normal active opens only validate.

- [ ] **Step 4: Replace connection context-only call sites touched by this slice**

Use:

```python
with contextlib.closing(
    open_operational_db(path, busy_ms=busy_ms, contract=contract)
) as database:
    with begin_immediate(database):
        database.execute(statement.sql, statement.parameters)
```

Do not treat `with connection:` as connection closure. Keep unrelated modules outside this slice unchanged until their owning plan modifies them.

- [ ] **Step 5: Run focused tests GREEN**

```bash
uv run --locked --no-sync pytest tests/test_operational_migrations.py tests/test_reliable_memory.py -q
uv run --locked --no-sync ruff check scripts/reliable_memory.py tests/test_operational_migrations.py tests/test_reliable_memory.py
```

Expected: PASS, including every subprocess killpoint.

- [ ] **Step 6: Optional checkpoint after explicit operator approval**

```bash
git add scripts/reliable_memory.py tests/test_operational_migrations.py tests/test_reliable_memory.py
git commit -m "fix: make operational migrations resumable"
```

### Task 3: Build The Queue V3 Candidate Schema

**Files:**
- Modify: `scripts/memory_queue.py`
- Modify: `tests/test_operational_migrations.py`
- Modify: `tests/test_memory_queue.py`
- Modify: `tests/test_memory_queue_migration.py`

- [ ] **Step 1: Write failing queue schema and v2-reconciliation tests**

Assert the fixed path, IDs, all normative tables, indexes, triggers, nine states, 1 MiB BLOB limit, 100-attempt ceiling, and exact source-row reconciliation. Add these fixtures:

- `test_queue_v3_fresh_schema_has_complete_invariant`: compare tables, columns, checks, indexes, triggers, and PRAGMAs with the declarations in this plan.
- `test_queue_v2_backup_migrates_canonical_text_payload_to_identical_blob`: seed canonical non-ASCII JSON text and assert exact UTF-8 BLOB bytes and digest survive.
- `test_queue_v2_hash_mismatch_is_preserved_dead_and_never_dispatched`: seed a wrong digest, migrate, and assert dead state, stable code, retained raw bytes/history, and no claim.
- `test_queue_partial_table_column_backfill_or_index_is_repaired`: parameterize each historical partial fixture and assert the full final invariant.
- `test_queue_candidate_uses_application_id_0x4c575133_and_user_version_3`: read both PRAGMAs from a closed-and-reopened candidate.

For a corrupt v2 row, preserve the exact UTF-8 bytes available from `payload_json`, preserve attempt history, set `state='dead'` and `error_code='payload_hash_mismatch'`, and do not invent a capture link.

- [ ] **Step 2: Run queue migration tests and verify RED**

```bash
uv run --locked --no-sync pytest tests/test_operational_migrations.py tests/test_memory_queue.py tests/test_memory_queue_migration.py -q
```

Expected: FAIL because `queue-v3.sqlite3` and its invariant do not exist.

- [ ] **Step 3: Add pure candidate initialization and validation functions**

Add module-level `initialize_queue_v3_candidate(path: Path, *, source_v2: Path | None, killpoint: Callable[[str], None] | None = None) -> dict[str, int]` and `validate_queue_v3_database(path: Path, *, state_root: Path) -> dict[str, object]`. Neither constructs `MemoryQueue`. The initializer creates or resumes only an unpublished candidate; the validator checks PRAGMAs, complete schema, foreign keys, payload bounds, and migrated row counts.

Declare an ordered tuple of `MigrationStatement` values. Rebuild v2 `tasks` into the normative table rather than trying to alter its state check or TEXT payload declaration. Keep the candidate unpublished until the full invariant, `integrity_check`, and `foreign_key_check` pass.

- [ ] **Step 4: Keep candidate construction isolated from active runtime paths**

Add a private test-only constructor seam that opens an explicitly supplied unpublished candidate after `validate_queue_v3_database()`. Do not change the normal `MemoryQueue.db_path` or import Task 5's adoption validator yet. Task 5 performs that switch atomically with the available pair validator, so this task can finish green without faking an adoption record.

- [ ] **Step 5: Run focused tests GREEN**

```bash
uv run --locked --no-sync pytest tests/test_operational_migrations.py tests/test_memory_queue.py tests/test_memory_queue_migration.py -q
uv run --locked --no-sync ruff check scripts/memory_queue.py tests/test_operational_migrations.py tests/test_memory_queue.py tests/test_memory_queue_migration.py
```

Expected: PASS for schema creation and candidate migration, with existing active-v2 runtime behavior unchanged until Task 5. No test remains intentionally red.

- [ ] **Step 6: Optional checkpoint after explicit operator approval**

```bash
git add scripts/memory_queue.py tests/test_operational_migrations.py tests/test_memory_queue.py tests/test_memory_queue_migration.py
git commit -m "feat: define queue v3 storage"
```

### Task 4: Build The Coordinator V3 Candidate Schema

**Files:**
- Modify: `scripts/markdown_transaction.py`
- Modify: `tests/test_operational_migrations.py`
- Modify: `tests/test_markdown_transaction.py`
- Modify: `tests/test_markdown_transaction_recovery.py`

- [ ] **Step 1: Write failing coordinator schema and partial-history tests**

Add these behavioral tests:

- `test_coordinator_v3_fresh_schema_has_complete_invariant`: compare the complete schema, triggers, indexes, and PRAGMAs with this plan.
- `test_coordinator_v2_rows_survive_table_rebuild_with_exact_operations`: seed every transaction state and operation kind and compare all migrated values and counts.
- `test_transaction_state_check_includes_aborting_and_aborted`: insert both new states and reject an unknown state.
- `test_partial_owner_fence_and_abort_columns_do_not_count_as_complete`: parameterize missing backfill, index, trigger, and schema marker fixtures.
- `test_coordinator_candidate_uses_application_id_0x4c575433_and_user_version_3`: read both PRAGMAs from a closed-and-reopened candidate.

Historical fixtures must include the narrow transaction state constraint, nullable backfill columns, missing indexes, a partial project-checkpoint migration, and a maintenance owner table with only the old four columns.

- [ ] **Step 2: Run coordinator migration tests and verify RED**

```bash
uv run --locked --no-sync pytest tests/test_operational_migrations.py tests/test_markdown_transaction.py tests/test_markdown_transaction_recovery.py -q
```

Expected: FAIL because the v3 coordinator schema is absent.

- [ ] **Step 3: Add pure candidate initialization and validation functions**

Add `initialize_coordinator_v3_candidate(path: Path, *, source_v2: Path | None, killpoint: Callable[[str], None] | None = None) -> dict[str, int]` and `validate_coordinator_v3_database(path: Path, *, state_root: Path) -> dict[str, object]`. The initializer creates or resumes only an unpublished coordinator candidate; the validator checks the complete schema, transactions, operations, ownership, fences, foreign keys, and PRAGMAs.

Rebuild constrained tables into operation-keyed temporary tables with explicit statement names. Copy old rows only after validating each old row. Preserve ambiguous historical rows as blockers; do not coerce them into healthy terminal states.

- [ ] **Step 4: Keep coordinator candidates isolated from the active constructor**

Add a private test-only coordinator seam that opens an explicitly supplied unpublished candidate after `validate_coordinator_v3_database()`. Leave the normal active path unchanged until Task 5 can install the pair validator and both path switches together. No Task 4 test fakes adoption.

- [ ] **Step 5: Run focused tests GREEN**

```bash
uv run --locked --no-sync pytest tests/test_operational_migrations.py tests/test_markdown_transaction.py tests/test_markdown_transaction_recovery.py -q
uv run --locked --no-sync ruff check scripts/markdown_transaction.py tests/test_operational_migrations.py tests/test_markdown_transaction.py tests/test_markdown_transaction_recovery.py
```

Expected: candidate creation and v2 reconciliation pass without `executescript()`.

- [ ] **Step 6: Optional checkpoint after explicit operator approval**

```bash
git add scripts/markdown_transaction.py tests/test_operational_migrations.py tests/test_markdown_transaction.py tests/test_markdown_transaction_recovery.py
git commit -m "feat: define coordinator v3 storage"
```

### Task 5: Implement Fresh Pair Creation And Explicit Offline Adoption

**Files:**
- Create: `scripts/installed_memory_repair.py`
- Create: `tests/test_reliability_v3_adoption.py`
- Modify: `scripts/memory_queue.py`
- Modify: `scripts/markdown_transaction.py`
- Modify: `tests/test_memory_queue.py`
- Modify: `tests/test_markdown_transaction.py`

- [ ] **Step 1: Write failing fresh, upgrade, tombstone, and crash tests**

Cover every row in the Pair Adoption crash matrix. Add frozen pre-v3 client subprocess fixtures that import a copy of the old queue/coordinator open logic and prove that, after adoption, opening `run/queue.sqlite3` or `run/markdown-transactions.sqlite3` fails with `DatabaseError` and cannot change either v3 database.

Required tests:

- `test_fresh_vault_creates_pair_tombstones_manifest_and_adoption`: assert exact paths, fresh tombstone branches, pair operation ID, schema hashes, and no retired files.
- `test_normal_startup_initializes_only_a_provably_empty_fresh_vault`: invoke each constructor first in separate fixtures, assert either can create the complete pair, and assert a concurrent observer of the manifest fails closed rather than joining mutation.
- `test_normal_startup_never_auto_adopts_existing_v2_databases`: instantiate both normal clients and assert no path or timestamp changed.
- `test_upgrade_retains_byte_identical_v2_databases`: hash source bytes before adoption and compare both fixed retired files afterward.
- `test_pair_uses_one_deterministic_operation_id`: recompute it from the canonical source/schema/integration descriptors and compare every artifact reference.
- `test_v3_mutation_is_disabled_until_complete_adoption_validates`: stop after each pre-adoption boundary and assert both normal constructors reject mutation.
- `test_interrupted_cutover_resumes_each_publication_boundary`: parameterize all Pair Adoption matrix boundaries and converge through explicit resume.
- `test_mismatched_partial_cutover_is_retained_and_reported`: mutate each resumable artifact independently and assert no overwrite or deletion.
- `test_completed_tombstones_block_frozen_v2_clients`: run frozen pre-v3 open logic in subprocesses and assert `DatabaseError` plus unchanged v3 hashes.
- `test_adoption_rejects_live_marker_inflight_lease_or_changed_source_identity`: parameterize every offline-gate blocker and assert no first publication.

- [ ] **Step 2: Run adoption tests and verify RED**

```bash
uv run --locked --no-sync pytest tests/test_reliability_v3_adoption.py -q
```

Expected: collection fails because `installed_memory_repair` is absent.

- [ ] **Step 3: Implement the read-only state classifier**

Add these internal records and functions:

```python
@dataclass(frozen=True)
class ReliabilityV3Paths:
    run: Path
    queue_legacy: Path
    coordinator_legacy: Path
    queue_active: Path
    coordinator_active: Path
    queue_retired: Path
    coordinator_retired: Path
    migration_record: Path
    adoption_record: Path


@dataclass(frozen=True)
class AdoptionInspection:
    state: Literal["fresh", "upgrade-required", "partial", "adopted", "conflict"]
    operation_id: str | None
    blockers: Sequence[str]
    details: Mapping[str, object]


```

Add `inspect_reliability_v3(*, root: Path, state_root: Path) -> AdoptionInspection`, `require_reliability_v3_adopted(*, root: Path, state_root: Path) -> Mapping[str, object]`, and internal `ensure_reliability_v3_ready(*, root: Path, state_root: Path) -> Mapping[str, object]`. Inspection performs only bounded reads. `require_reliability_v3_adopted` accepts only `state == "adopted"`, revalidates every referenced path identity/hash/schema/PRAGMA, and returns the validated adoption record; otherwise it raises the inspection's stable blocker. `ensure_reliability_v3_ready` calls `require` for adopted state, invokes Step 4 only for an exact `fresh` inspection, and rejects `upgrade-required`, `partial`, and `conflict` without mutation.

Inspection never creates `run/`, opens a writable database, hardens permissions, rewrites a file, or updates a timestamp. `require_reliability_v3_adopted()` validates the final record and all referenced evidence every mutating startup; it never infers that pre-v3 processes are absent.

- [ ] **Step 4: Implement deterministic fresh initialization**

A vault is fresh only when both legacy paths, both active v3 paths, both retired paths, both evidence records, and operation-keyed candidates are absent, and no legacy queue/transaction artifacts imply prior operational use. Implement internal `initialize_fresh_reliability_v3(*, root: Path, state_root: Path, expected: AdoptionInspection, killpoint: Callable[[str], None] | None = None) -> Mapping[str, object]`. Recheck the exact fresh snapshot, compute the operation ID, and publish the immutable migration manifest before the first candidate or tombstone. Then create and validate both candidates, prepare both fresh tombstones, publish each legacy tombstone and corresponding active database in the manifest order, revalidate the complete pair, and publish adoption last. Any crash after the manifest leaves a classified `partial`; only explicit repair resumes it. A crash before the manifest leaves no publication and remains `fresh`. A racing normal process that observes the manifest reports partial state and does not resume the creator's operation.

- [ ] **Step 5: Implement the offline upgrade gate and pair cutover**

The mutating adoption function has signature `adopt_reliability_v3(*, root: Path, state_root: Path, confirm_all_agents_stopped: bool, killpoint: Callable[[str], None] | None = None) -> Mapping[str, object]`.

Require `confirm_all_agents_stopped is True`. Reject a live `compile.pid`, live `maintenance.lock`, any in-flight queue lease, any v2 writer/project/maintenance owner, unsafe database sidecar, unknown liveness, changed source path identity, or changed source hash. Use one operation ID over canonical source descriptors, schema digests, and installed integration digest. Publish the immutable migration manifest and read it back before retired-copy, candidate, tombstone, or active-path publication. Keep the vault offline until adoption exists.

For each database: hold an exclusive source transaction while making and verifying a byte-for-byte retired copy; use `Connection.backup()` to create the candidate; close every source and target connection; validate the candidate; revalidate source identity/hash; replace the legacy path with exact prepared tombstone bytes; publish candidate to the active v3 path. Apply the platform publication contract at every boundary.

- [ ] **Step 6: Implement the installer backend functions**

`inspect_installed_vault()` returns closed keys `mode`, `overall_status`, `actions`, `blockers`, and `details`. `repair_installed_vault()` initiates or resumes an upgrade only when both booleans are true. Without adoption selection, explicit apply may resume an exact partial fresh manifest created by `initialize_fresh_reliability_v3()` and already-authorized post-adoption operations; it cannot initiate or resume a v2 upgrade, discard, corrupt disposition, purge, or runtime deletion.

- [ ] **Step 7: Run adoption and constructor tests GREEN**

Before this run, switch normal `MemoryQueue.db_path` to `run/queue-v3.sqlite3` and normal `MarkdownCoordinator.database_path` to `run/markdown-transactions-v3.sqlite3`. Both constructors call `ensure_reliability_v3_ready(root=resolved_vault_root, state_root=resolved_state_root)` before opening or mutating either database. Resolve both roots through the existing worktree-aware environment contract, never the current working directory. Candidate-only test seams remain explicit and private.

```bash
uv run --locked --no-sync pytest tests/test_reliability_v3_adoption.py tests/test_memory_queue.py tests/test_markdown_transaction.py -q
uv run --locked --no-sync ruff check scripts/installed_memory_repair.py scripts/memory_queue.py scripts/markdown_transaction.py tests/test_reliability_v3_adoption.py
```

Expected: fresh initialization, explicit upgrade, every partial resume, and old-client tombstone tests pass.

- [ ] **Step 8: Optional checkpoint after explicit operator approval**

```bash
git add scripts/installed_memory_repair.py scripts/memory_queue.py scripts/markdown_transaction.py tests/test_reliability_v3_adoption.py tests/test_memory_queue.py tests/test_markdown_transaction.py
git commit -m "feat: adopt reliability v3 databases offline"
```

### Task 6: Implement Canonical All-Role Ownership

**Files:**
- Create: `scripts/operational_ownership.py`
- Create: `tests/test_operational_ownership.py`
- Modify: `scripts/install_pyright.py`
- Modify: `tests/test_install_pyright.py`

- [ ] **Step 1: Write failing process identity and canonical lease tests**

Cover Linux boot ID/start ticks, Windows process creation time, Darwin process start time, PID reuse, access denied, missing process, expired-live, expired-dead, exact-token heartbeat/release, fencing epochs, all closed roles, and `runtime-deletion-check` exclusion. The named regressions are:

- `test_takeover_requires_expiry_and_positive_process_death`: cross product lease live/expired with process alive/dead/unknown; only expired/dead succeeds.
- `test_pid_reuse_is_dead_only_when_start_identity_differs`: same PID plus different start identity permits expired takeover, while same identity remains live.
- `test_denied_liveness_is_unknown_and_blocks_takeover`: injected access denial yields `owner_liveness_unknown` and preserves the row.
- `test_heartbeat_and_release_verify_one_affected_row`: stale token, epoch, PID, and start identity each affect zero rows and raise.
- `test_runtime_deletion_check_requires_zero_other_canonical_owners`: one fixture per role blocks snapshot admission.
- `test_every_other_acquisition_rejects_runtime_deletion_check`: while the snapshot owner is live, every other role fails before inserting a row.

- [ ] **Step 2: Run focused ownership tests and verify RED**

```bash
uv run --locked --no-sync pytest tests/test_operational_ownership.py tests/test_install_pyright.py -q
```

Expected: FAIL because the shared ownership module does not exist.

- [ ] **Step 3: Move process-start identity behind one public contract**

Implement:

```python
OwnerRole = Literal[
    "capture", "project", "markdown-writer", "queue-worker", "compile",
    "doctor", "nightly", "weekly", "lsp", "queue-operator", "repair",
    "runtime-deletion-check",
]
ProcessState = Literal["alive", "dead", "unknown"]


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_identity: str


@dataclass(frozen=True)
class OwnerLease:
    state_root: Path
    role: OwnerRole
    scope: str
    actor_id: str
    token: str
    epoch: int
    process: ProcessIdentity
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    ttl_seconds: int
    heartbeat_seconds: int


```

Export `current_process_identity() -> ProcessIdentity`, `process_identity_state(identity: ProcessIdentity) -> ProcessState`, and `current_actor_identity() -> str` with the platform behavior below. Each function raises a stable unsupported-platform error rather than returning guessed identity.

`current_actor_identity()` returns `posix-uid:<decimal>` or a bounded `windows-sid:<SID>`. `install_pyright.py` imports the shared process-start function; leave one compatibility alias only where existing tests or external imports prove it is needed.

- [ ] **Step 4: Implement `OwnershipRegistry`**

Implement `OwnershipRegistry` with these exact public signatures:

- `__init__(self, state_root: Path, *, clock: Callable[[], datetime] = utc_now, process_probe: Callable[[ProcessIdentity], ProcessState] = process_identity_state) -> None`
- `acquire(self, role: OwnerRole, *, scope: str, actor_id: str | None = None, token: str | None = None, marker: MarkerIdentity | None = None) -> OwnerLease`
- `heartbeat(self, lease: OwnerLease) -> OwnerLease`
- `require(self, database: sqlite3.Connection, lease: OwnerLease) -> None`
- `release(self, lease: OwnerLease) -> None`

Derive lease timing only from the role table above. Acquisition uses one `BEGIN IMMEDIATE`, rejects a live deletion-check row first, and increments `maintenance_owner_epochs`. Existing same-scope owner is replaceable only when expired and positively dead. Heartbeat compares role, scope, actor, token, epoch, PID, start identity, and unexpired row. Release deletes only the exact row and requires `rowcount == 1`.

- [ ] **Step 5: Add marker identity records**

```python
@dataclass(frozen=True)
class MarkerIdentity:
    relative_path: str
    sha256: str
    file_identity: RuntimeFileIdentity
    pid: int
```

Validate marker digest and path identity before cleanup. Unknown liveness or changed identity is a blocker, never a stale-marker deletion signal.

- [ ] **Step 6: Run focused ownership tests GREEN**

```bash
uv run --locked --no-sync pytest tests/test_operational_ownership.py tests/test_install_pyright.py -q
uv run --locked --no-sync ruff check scripts/operational_ownership.py scripts/install_pyright.py tests/test_operational_ownership.py tests/test_install_pyright.py
```

Expected: all process and lease states pass on the current platform; platform-specific probes remain separately CI-qualified.

- [ ] **Step 7: Optional checkpoint after explicit operator approval**

```bash
git add scripts/operational_ownership.py scripts/install_pyright.py tests/test_operational_ownership.py tests/test_install_pyright.py
git commit -m "feat: add canonical operational ownership"
```

### Task 7: Project Ownership Into Every Existing Domain

**Files:**
- Modify: `scripts/markdown_transaction.py`
- Modify: `scripts/project_journal.py`
- Modify: `scripts/memory_queue.py`
- Modify: `scripts/lsp_process.py`
- Modify: `scripts/maybe_compile.py`
- Modify: `scripts/scheduled_nightly.py`
- Modify: `scripts/scheduled_weekly.py`
- Modify: `tests/test_operational_ownership.py`
- Modify: `tests/test_markdown_transaction.py`
- Modify: `tests/test_project_journal.py`
- Modify: `tests/test_memory_queue_races.py`
- Modify: `tests/test_lsp_process.py`
- Modify: `tests/test_maybe_compile.py`
- Modify: `tests/test_scheduled_nightly.py`
- Create: `tests/test_scheduled_weekly.py`

- [ ] **Step 1: Write failing direct, nested, and projection-order tests**

Add behavioral tests for all of these cases:

- `test_direct_project_acquisition_inserts_canonical_and_domain_rows_atomically`: inject before/after each insert and prove no committed project domain row lacks the same canonical token and epoch.
- `test_nested_writer_references_parent_without_reacquiring_or_releasing_it`: count registry mutations and prove nested exit removes only its exact `writer_owners` row.
- `test_queue_projection_failure_releases_only_the_exact_canonical_token`: pause after canonical insertion, install a successor after exact cleanup, and prove failed acquisition cannot delete it.
- `test_queue_release_removes_projection_before_canonical_admission`: inject at both release boundaries and assert Doctor sees either the queue projection or canonical row.
- `test_lsp_publication_uses_canonical_token_and_epoch_in_owner_and_lease_records`: compare registry, immutable owner file, and mutable lease file throughout process lifetime.
- `test_compile_marker_stays_three_lines_and_is_published_before_canonical_owner`: run the frozen v2 parser at both acquisition boundaries and prove mutual exclusion.
- `test_maintenance_marker_remains_one_ascii_decimal_pid`: run frozen nightly and weekly parsers against the live v3 marker and prove neither deletes it.
- `test_weekly_keeps_outer_owner_and_marker_while_running_nested_nightly_work`: barrier every phase and assert one unchanged outer token, epoch, marker identity, and lease.

Freeze the old `compile.pid` and `maintenance.lock` parser logic in subprocess fixtures. Do not merely inspect source text.

- [ ] **Step 2: Run focused projection tests and verify RED**

```bash
uv run --locked --no-sync pytest tests/test_operational_ownership.py tests/test_markdown_transaction.py tests/test_project_journal.py tests/test_memory_queue_races.py tests/test_lsp_process.py tests/test_maybe_compile.py tests/test_scheduled_nightly.py tests/test_scheduled_weekly.py -q
```

Expected: FAIL because domain acquisitions are still independent and weekly still yields its marker.

- [ ] **Step 3: Make writer and project projections atomic in the coordinator DB**

Use these exact public signatures:

- `writer_gate(self, *, owner: OwnerLease | None = None, wait_seconds: float | None = None) -> Iterator[OwnerLease]`, decorated with `@contextlib.contextmanager`.
- `acquire_lease(self, slug: str, owner: str, ttl: int = 30, *, token: str | None = None, now: datetime | None = None, ownership: OwnerLease | None = None) -> ProjectLease`.

A top-level writer/project acquisition inserts both rows in one `BEGIN IMMEDIATE`. A nested writer verifies parent role/scope/token/epoch/process identity and inserts only `writer_owners`. Reentrant writer depth reuses the same domain row. Nested release removes only the exact writer projection. Direct release removes domain first and canonical second in one transaction because both live in the coordinator database.

- [ ] **Step 4: Add queue projection lifecycle**

Use the `queue_ownership` schema declared above and implement `queue_owner(self, *, role: Literal["queue-worker", "queue-operator"], scope: str, parent: OwnerLease | None = None) -> Iterator[OwnerLease]`, decorated with `@contextlib.contextmanager`. With no parent, worker/operator acquires the matching canonical role. A worker parent may be `queue-worker`, `compile`, `doctor`, `nightly`, or `weekly`; an operator parent may be `queue-operator` or `repair`. Any other role is rejected before projection.

Top-level order is canonical first, projection second; cleanup on projection failure releases only that exact canonical lease. Controlled release removes projection first and canonical last. Nested work inserts/removes only the queue projection referencing the parent lease. A canonical-only or projection-only crash state blocks Doctor until expiry plus positive death proof.

- [ ] **Step 5: Project LSP ownership without changing process containment**

Acquire canonical `lsp` before creating the owner scratch directory. Add `canonical_role`, `canonical_scope`, `actor_id`, `owner_token`, `fencing_epoch`, and `process_start_identity` to `owner.json` and mutable `lease.json`. Keep kill-on-close Job Object and POSIX process-group behavior unchanged. Controlled release removes `lease.json` first, then canonical admission; immutable owner/failure evidence keeps its existing retention behavior.

- [ ] **Step 6: Implement marker-first compatibility acquisition**

Add `acquire_compile_owner(*, state_root: Path) -> tuple[OwnerLease, MarkerIdentity]` and `acquire_scheduled_owner(role: Literal["nightly", "weekly"], *, state_root: Path) -> tuple[OwnerLease, MarkerIdentity]`.

Generate actor ID and requested canonical token before marker creation. `compile.pid` bytes are exactly `<pid>\n<ISO-seconds>\n<owner-token>\n`. `maintenance.lock` bytes are exactly the current decimal PID with no JSON or token. Capture marker digest and full path identity. On acquisition failure, remove only the same identity/digest/PID. On normal release, clear canonical admission first and the exact marker last. A live expired marker is not reclaimed automatically.

- [ ] **Step 7: Keep scheduled ownership for the complete run**

Refactor `scheduled_nightly.main()` and `scheduled_weekly.main()` through callable `run_nightly(*, ownership)` and `run_weekly(*, ownership)` bodies. Weekly calls nightly work in-process with its outer lease and never unlinks/recreates `maintenance.lock`. Nested queue, compile, writer, archive, and generation work receive the outer lease. Any remaining subprocess must finish with a terminal exit status before outer release; no detached child owns unfinished maintenance.

- [ ] **Step 8: Run projection and compatibility tests GREEN**

```bash
uv run --locked --no-sync pytest tests/test_operational_ownership.py tests/test_markdown_transaction.py tests/test_project_journal.py tests/test_memory_queue_races.py tests/test_lsp_process.py tests/test_maybe_compile.py tests/test_scheduled_nightly.py tests/test_scheduled_weekly.py -q
uv run --locked --no-sync ruff check scripts/markdown_transaction.py scripts/project_journal.py scripts/memory_queue.py scripts/lsp_process.py scripts/maybe_compile.py scripts/scheduled_nightly.py scripts/scheduled_weekly.py tests/test_operational_ownership.py tests/test_scheduled_weekly.py
```

Expected: all domain rows agree with canonical admission, and frozen v2 marker parsers interoperate.

- [ ] **Step 9: Optional checkpoint after explicit operator approval**

```bash
git add scripts/markdown_transaction.py scripts/project_journal.py scripts/memory_queue.py scripts/lsp_process.py scripts/maybe_compile.py scripts/scheduled_nightly.py scripts/scheduled_weekly.py tests/test_operational_ownership.py tests/test_markdown_transaction.py tests/test_project_journal.py tests/test_memory_queue_races.py tests/test_lsp_process.py tests/test_maybe_compile.py tests/test_scheduled_nightly.py tests/test_scheduled_weekly.py
git commit -m "feat: project canonical runtime ownership"
```

### Task 8: Enforce Canonical Payload Identity And Exact Dedupe

**Files:**
- Modify: `scripts/memory_queue.py`
- Modify: `tests/test_memory_queue.py`
- Modify: `tests/test_memory_queue_races.py`
- Modify: `tests/test_memory_queue_cli.py`
- Modify: `tests/test_reliable_memory_schemas.py`

- [ ] **Step 1: Write the every-transition corruption matrix**

Parameterize mutation of either `payload_blob` or `input_hash` immediately before each operation:

```text
insertion validation
claim
lease heartbeat
lease expiry
execute handoff
result adoption
result publication
acknowledge
failure/retry
cancellation
ordinary export
redrive
dead-lettering
migration reconciliation
ordinary purge
```

For every case assert no payload is dispatched, parsed into work, copied to a replacement task, exported by ordinary purge, or reported as successful. The only automatic transition is `dead/payload_hash_mismatch`; explicit corrupt handling starts in Task 11.

Add exact dedupe tests:

- `test_exact_dedupe_returns_existing_task_only_for_kind_version_and_hash_match`: enqueue twice with one key, revalidate the existing BLOB in the conflict transaction, and assert one unchanged row and the original ID.
- `test_dedupe_conflicts_on_kind_handler_or_payload_difference`: parameterize each identity field, assert `QueueOperationError("dedupe_conflict")`, and prove the existing row was not updated.
- `test_exporter_round_trips_real_queue_task_through_queue_task_v3_schema`: populate lease, result, attempt, lineage, and capture fields through production APIs, export, validate, decode, and compare the closed object.
- `test_blocked_attempt_history_stops_at_the_hard_limit`: repeat block/unblock with a restored retry ordinal and assert the 101st dispatch is prevented and retained as `dead/attempt_history_exhausted`.
- `test_ordinary_purge_uses_one_transactional_authorization_without_schema_mutation`: trace SQL, assert no trigger is dropped or recreated, and prove authorization/evidence/task deletes commit together.

- [ ] **Step 2: Run queue identity tests and verify RED**

```bash
uv run --locked --no-sync pytest tests/test_memory_queue.py tests/test_memory_queue_races.py tests/test_memory_queue_cli.py tests/test_reliable_memory_schemas.py -q
```

Expected: FAIL at claim/dedupe/export because current code trusts stored hash and aliases any duplicate key.

- [ ] **Step 3: Add one bounded payload validator**

```python
@dataclass(frozen=True)
class PayloadValidation:
    raw: bytes
    input_hash: str
    payload: dict[str, object] | None
    code: str | None
```

Add `validate_payload_blob(raw: bytes, stored_hash: str, *, parse: bool) -> PayloadValidation`.

Reject over 1 MiB before decode. Hash memoryview chunks. Verify stored lowercase hash. Decode strict UTF-8. Call `json.loads` with `object_pairs_hook` that rejects duplicate keys, `parse_constant` that rejects `NaN`/infinities, and a `parse_int` callback that rejects an integer token longer than the bounded input policy before conversion. Then recursively enforce depth 32, each UTF-8 string/key at most 256 KiB, each array/object at most 1,024 members, string keys only, no floats or booleans masquerading as integers, and canonical re-encoding equal to stored bytes. Catch recursion, integer conversion, Unicode, and JSON failures as stable `payload_hash_mismatch`; never include raw bytes in diagnostics.

Normal transitions call with `parse=True`. Corrupt detection/export calls with `parse=False` and uses only bounded raw bytes and digests.

- [ ] **Step 4: Centralize transition validation and corrupt demotion**

Add `_require_valid_task_payload(self, connection: sqlite3.Connection, row: sqlite3.Row, *, parse: bool) -> PayloadValidation` and `_demote_payload_mismatch(self, connection: sqlite3.Connection, row: sqlite3.Row, *, now: datetime) -> None`.

Every transition in Step 1 must invoke the helper inside its `BEGIN IMMEDIATE` transaction before state change. Verify affected row counts for every update. `claim()` skips no corrupt row silently: it demotes the selected row and continues a bounded selection loop. Before leasing, it also verifies fewer than 100 immutable attempt-history rows; the bound failure uses the metadata-only dead transition and never dispatches payload.

- [ ] **Step 5: Implement exact dedupe**

Inside the enqueue `BEGIN IMMEDIATE`, when `dedupe_key` is non-null, first select existing `id`, `kind`, `handler_version`, `payload_blob`, and `input_hash` by exact key. The write lock prevents a competing insert between select and insert. If absent, insert and require one affected row. If present, revalidate its BLOB/hash/canonical form and return the existing ID only when kind, handler version, hash, and canonical bytes all match the proposal. Otherwise raise `QueueOperationError("dedupe_conflict")`. Never catch a broad integrity error as dedupe and never update the existing row.

- [ ] **Step 6: Make the production exporter the schema authority**

Change `_export_task_record()` to emit exactly the `queue-task/v3` shape. Include result operation ID, lineage generation, attempt history up to 100, sorted source links, and active capture binding. Validate the produced object against `queue-task-v3.json` before canonical encoding in exports and tests. Keep 64 MiB aggregate metadata and 16 MiB per result ceilings. After a verified ordinary export, derive one operation ID and authorization digest from its manifest; for each selected task, insert `ordinary` authorization, delete authorized task-owned history/link evidence and task, and remove authorization in one transaction. Never drop or recreate an immutability trigger at runtime.

- [ ] **Step 7: Run queue identity tests GREEN**

```bash
uv run --locked --no-sync pytest tests/test_memory_queue.py tests/test_memory_queue_races.py tests/test_memory_queue_cli.py tests/test_reliable_memory_schemas.py -q
uv run --locked --no-sync ruff check scripts/memory_queue.py tests/test_memory_queue.py tests/test_memory_queue_races.py tests/test_memory_queue_cli.py tests/test_reliable_memory_schemas.py
```

Expected: every corruption case fails closed, exact duplicates alias, conflicts do not, and a real export validates.

- [ ] **Step 8: Optional checkpoint after explicit operator approval**

```bash
git add scripts/memory_queue.py tests/test_memory_queue.py tests/test_memory_queue_races.py tests/test_memory_queue_cli.py tests/test_reliable_memory_schemas.py
git commit -m "fix: enforce queue payload identity"
```

### Task 9: Add Capture Links, Resolutions, Seals, Decisions, And Dual Fences

**Files:**
- Modify: `scripts/memory_queue.py`
- Modify: `scripts/markdown_transaction.py`
- Create: `tests/test_queue_v3_capture_links.py`
- Modify: `tests/test_memory_queue_races.py`
- Modify: `tests/test_markdown_transaction.py`

- [ ] **Step 1: Write failing link and decision authority tests**

Cover atomic enqueue/link insertion, index/file/link disagreement, missing/conflicting link export-only behavior, append-only resolution, supersession before consumption, first-consumer seal, post-seal conflict evidence, and all three decision stages.

- `test_capture_enqueue_atomically_inserts_task_and_immutable_link`: crash before and after each statement and assert both rows commit together or neither exists.
- `test_corrupt_payload_never_supplies_intent_identity`: place a different intent ID only in corrupt bytes and assert no intent-fence acquisition is attempted.
- `test_resolution_none_leaves_task_nonterminal_and_export_only`: append the `none` resolution and assert unchanged task state, no intent fence, and corrupt-export eligibility only.
- `test_first_consumer_seals_active_digest_before_side_effect`: pause after seal commit, race a superseding resolution, and assert the resolution loses before the side-effect callback runs.
- `test_semantic_decision_seal_and_index_commit_together`: publish exact candidate bytes, race a resolution at the queue transaction, and assert either both immutable rows bind the old digest or neither row exists and the candidate file remains conflict evidence.
- `test_semantic_decision_never_populates_terminal_result_or_succeeds_task`: parameterize all three stages, expire the lease, and assert result fields remain null and task is not succeeded.

- [ ] **Step 2: Write failing task/intent race tests**

Test worker versus operator before and after task fence acquisition, between task and intent acquisition, before decision seal, before transaction precondition, before terminal publication, and during release. Assert the only acquisition order is task first, intent second; release is intent first, task last.

- [ ] **Step 3: Run focused tests and verify RED**

```bash
uv run --locked --no-sync pytest tests/test_queue_v3_capture_links.py tests/test_memory_queue_races.py tests/test_markdown_transaction.py -q
```

Expected: collection or assertions fail because links, seals, decisions, and intent fences are absent.

- [ ] **Step 4: Implement immutable link identity and active-resolution lookup**

Define link digest over canonical JSON:

```python
{
    "schema_version": "capture-task-link/v1",
    "task_id": task_id,
    "intent_id": intent_id,
    "intent_sha256": intent_sha256,
    "handler_version": handler_version,
}
```

Define each resolution digest over its full `capture-task-link-resolution/v1` record. Add this record:

```python
@dataclass(frozen=True)
class CaptureTaskBinding:
    task_id: str
    intent_id: str | None
    intent_sha256: str | None
    handler_version: int
    active_digest: str
    seal_digest: str | None


@dataclass(frozen=True)
class SemanticDecision:
    task_id: str
    intent_id: str
    stage: Literal["flush", "feedback", "feedback-verify"]
    decision_path: str
    decision_sha256: str
    active_link_digest: str
    seal_digest: str
    published_at: datetime
```

Implement `active_capture_binding(self, connection: sqlite3.Connection, task_id: str) -> CaptureTaskBinding` to return exactly one valid original or resolution leaf and otherwise fail closed.

Validate link against `capture_intents` row and authoritative ready file descriptor supplied by the capture caller. Missing/conflicting authority raises `capture_link_conflicted`. A repair resolution requires canonical `repair` owner, task quiescence, intent quiescence when selecting an intent, current UID/SID, and 1-4096 UTF-8 byte reason.

- [ ] **Step 5: Implement first-consumer seals and semantic decision indexing**

Seal insertion and resolution append/supersession serialize in the same queue transaction. An existing identical seal is idempotent; any different consumer or active digest is `capture_link_sealed`. Add immutable triggers. `publish_semantic_decision()` reads back the create-only candidate file through the capture slice callback, revalidates the live intent fence immediately before the queue transaction, then atomically inserts or verifies both the `semantic-decision` seal and `semantic_decisions` row under the exact queue projection/task fence. Retry accepts only the same task, intent, stage, path, digest, active link, and seal; a mismatch is retained conflict evidence. It leaves task lease/result/state unchanged.

- [ ] **Step 6: Implement task and intent fence APIs**

Queue record and API:

```python
@dataclass(frozen=True)
class TaskFence:
    task_id: str
    mode: Literal["worker", "queue-operator"]
    token: str
    epoch: int
    owner: OwnerLease
    expires_at: datetime
```

Add `acquire_task_fence(self, task_id: str, *, mode: Literal["worker", "queue-operator"], owner: OwnerLease) -> TaskFence`.

Coordinator record and API:

```python
@dataclass(frozen=True)
class IntentFence:
    intent_id: str
    mode: Literal["capture", "worker", "operator"]
    token: str
    epoch: int
    owner: OwnerLease
    expires_at: datetime
```

Add `acquire_intent_fence(self, intent_id: str, *, mode: Literal["capture", "worker", "operator"], owner: OwnerLease) -> IntentFence`.

Mode-to-owner validation is closed: task/intent `worker` requires a worker projection whose canonical role is `queue-worker`, `compile`, `doctor`, `nightly`, or `weekly`; task `queue-operator` and intent `operator` require an operator/repair projection whose canonical role is `queue-operator` or `repair`; intent `capture` requires canonical `capture`. Heartbeat and release compare exact fence token/epoch plus canonical actor/token/epoch/process identity and one affected row. Before a queue mutation, verify the canonical owner in the coordinator; inside the queue transaction, verify the exact queue projection, task fence, and queue lease. Queue release always deletes that projection before canonical release, so the locked projection is the transaction-local cross-database fence and no code claims an atomic read across both databases. Markdown preconditions can verify intent fence, parent canonical owner, and sealed coordinator projection together because all three are in the coordinator database.

- [ ] **Step 7: Implement the dual-fence context manager**

Implement `capture_task_fences(queue: MemoryQueue, coordinator: MarkdownCoordinator, task_id: str, *, intent_id: str | None, mode: Literal["worker", "queue-operator"], owner: OwnerLease) -> Iterator[tuple[TaskFence, IntentFence | None]]`, decorated with `@contextlib.contextmanager`.

If second acquisition fails, release only the exact first token. Taskless overflow/discard uses operator intent fence alone and rechecks no task link exists. A missing/conflicting link never produces an intent ID and is export-only until explicit resolution.

- [ ] **Step 8: Project sealed binding into coordinator**

Add `project_capture_binding(binding, *, intent_fence)` to `MarkdownCoordinator`. It inserts or verifies `capture_binding_projections` under the exact live intent fence. Recovery may repeat this projection after queue seal. A mismatch is a conflict; it never rewrites the queue seal.

- [ ] **Step 9: Run focused tests GREEN**

```bash
uv run --locked --no-sync pytest tests/test_queue_v3_capture_links.py tests/test_memory_queue_races.py tests/test_markdown_transaction.py -q
uv run --locked --no-sync ruff check scripts/memory_queue.py scripts/markdown_transaction.py tests/test_queue_v3_capture_links.py tests/test_memory_queue_races.py tests/test_markdown_transaction.py
```

Expected: link authority, decision separation, sealing, projections, and every race boundary pass.

- [ ] **Step 10: Optional checkpoint after explicit operator approval**

```bash
git add scripts/memory_queue.py scripts/markdown_transaction.py tests/test_queue_v3_capture_links.py tests/test_memory_queue_races.py tests/test_markdown_transaction.py
git commit -m "feat: bind capture tasks to fenced authority"
```

### Task 10: Add Rollback-Only Transaction Abort Recovery

**Files:**
- Modify: `scripts/markdown_transaction.py`
- Create: `tests/test_transaction_abort.py`
- Modify: `tests/test_markdown_transaction_recovery.py`
- Modify: `tests/test_runtime_deletion_contract.py`

- [ ] **Step 1: Write failing abort state and receipt tests**

Cover every Transaction Abort crash-matrix row and every target kind (`create`, `replace`, `delete`) in mixed before/after states. Include receipt conflict, missing receipt after `aborted`, expired operator fence, committed transaction, and conflicted/quarantined refusal.

- `test_abort_atomically_commits_aborting_direction_before_restore`: kill immediately after the direction transaction and prove normal recovery never applies an after image.
- `test_aborting_recovery_restores_and_verifies_all_before_images`: parameterize before, after, and third-party bytes for every operation kind; only the first two converge.
- `test_abort_receipt_is_create_only_bounded_and_read_back_verified`: verify exact schema fields, 64 KiB rejection, conflicting existing bytes, file identity, and recorded digest.
- `test_terminal_aborted_requires_exact_receipt_and_restored_tree`: independently remove or alter receipt, manifest, and each restored target and assert a corruption blocker.

Parameterize killpoints `after_aborting`, `after_each_abort_target`, `before_abort_receipt`, `after_abort_receipt`, and `before_aborted`.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run --locked --no-sync pytest tests/test_transaction_abort.py tests/test_markdown_transaction_recovery.py tests/test_runtime_deletion_contract.py -q
```

Expected: FAIL because transaction states and abort API are absent.

- [ ] **Step 3: Implement deterministic abort records**

Add:

```python
@dataclass(frozen=True)
class TransactionAbortReceipt:
    transaction_id: str
    intent_id: str
    abort_operation_id: str
    receipt_path: str
    receipt_sha256: str
    aborted_at: str
```

Add `abort_for_discard(self, transaction_id: str, *, intent_fence: IntentFence, active_link_digest: str, actor_identity: str, deadline: float = float("inf"), cancelled: Callable[[], bool] | None = None) -> TransactionAbortReceipt`.

The deterministic abort operation ID hashes transaction ID, intent ID, active link digest, fence epoch, and before-image manifest. Choose and persist `abort_chosen_at` when entering `aborting`; retry uses it so receipt bytes are identical. Store only `sha256(fence.token)` in the receipt, not the secret token.

- [ ] **Step 4: Implement rollback and receipt publication**

In one coordinator transaction, verify operator intent fence, sealed binding projection, transaction state, and no commit authority; persist `aborting`, operation ID, chosen time, and manifest hash. Restore operations in reverse order. Accept current before hash as already restored and current after hash as restorable; any third hash leaves `aborting` with `abort_target_conflict`.

Publish `run/transactions/<transaction-id>/abort-receipt.json` create-only using `transaction-abort/v1`, owner-only permissions, 64 KiB cap, checked file/parent sync, and read-back digest. A final `BEGIN IMMEDIATE` rechecks fence, receipt, manifest, every target hash, and sealed binding before changing exactly one row to `aborted`.

- [ ] **Step 5: Make normal recovery rollback-only for `aborting`**

Process `aborting` before forward states. Adopt valid receipt into `aborted`; resume restore without receipt; quarantine a conflicting receipt. Validate every `aborted` row's exact receipt during recovery and Doctor inspection. Add `aborting` to nonterminal deletion blockers and retain `aborted` receipt evidence for the normal 30-day transaction window.

- [ ] **Step 6: Run focused tests GREEN**

```bash
uv run --locked --no-sync pytest tests/test_transaction_abort.py tests/test_markdown_transaction_recovery.py tests/test_runtime_deletion_contract.py -q
uv run --locked --no-sync ruff check scripts/markdown_transaction.py tests/test_transaction_abort.py tests/test_markdown_transaction_recovery.py tests/test_runtime_deletion_contract.py
```

Expected: every abort killpoint converges to receipt-backed `aborted` or an explicit blocker, never forward commit after `aborting`.

- [ ] **Step 7: Optional checkpoint after explicit operator approval**

```bash
git add scripts/markdown_transaction.py tests/test_transaction_abort.py tests/test_markdown_transaction_recovery.py tests/test_runtime_deletion_contract.py
git commit -m "feat: recover transaction aborts durably"
```

### Task 11: Implement Fenced Corrupt-Task Quarantine And Paged Export

**Files:**
- Modify: `scripts/memory_queue.py`
- Create: `tests/test_queue_v3_corruption.py`
- Modify: `tests/test_memory_queue_cli.py`
- Modify: `tests/test_runtime_deletion_contract.py`

- [ ] **Step 1: Write failing quarantine, path, and bounded-page tests**

Use task IDs containing traversal, Windows reserved names, mixed case, separators, Unicode normalization aliases, and 256 UTF-8 bytes. Assert every package name is exactly `corrupt-<64 lowercase hex>` and no source identifier is a path component.

Add fan-out fixtures larger than 2,500 links and these tests:

- `test_quarantine_first_freezes_generation_and_moves_to_quarantine_pending`: pause after the first transaction and assert all incoming-link mutations fail before any package I/O.
- `test_each_invocation_exports_at_most_1000_links_1mib_or_5_seconds`: parameterize each limit, count selected rows and bytes, inject a monotonic clock, and assert no query fetches the full fan-out.
- `test_final_disposition_restreams_all_cas_inputs_and_publishes_state_last`: mutate generation, count, metadata, raw bytes, and history independently and assert no disposition row or terminal state.
- `test_missing_or_conflicting_capture_link_is_export_only`: run both cases, assert no intent fence call, and prove package export succeeds while disposition remains blocked.
- `test_nonmatching_orphan_package_is_a_retained_blocker`: alter each fixed package file independently and assert retry preserves it and returns `orphan_corrupt_package_conflict`.
- `test_superseded_package_disposition_is_bounded_resumable_and_non_destructive`: parameterize an empty package and more than 2,500 valid fixed files, bind the final root across invocations, and prove neither the original package nor any file is renamed or deleted.

- [ ] **Step 2: Add failing CLI trust-boundary tests**

Extend choices with `quarantine-corrupt` and `supersede-corrupt-package`. Quarantine requires task ID and `--reason`; supersession requires exactly one lowercase 64-hex package key and `--reason`. Reject empty or over-4,096-byte reasons before database work. Output only stable code, requested task/package key, state, operation ID, page count, and completion boolean. Never print payload, history, actor identity, reason, filesystem exception text, or discovered source IDs.

- [ ] **Step 3: Run focused tests and verify RED**

```bash
uv run --locked --no-sync pytest tests/test_queue_v3_corruption.py tests/test_memory_queue_cli.py tests/test_runtime_deletion_contract.py -q
```

Expected: FAIL because corrupt tasks have no explicit export/disposition path.

- [ ] **Step 4: Implement operation identity and first fenced transition**

Add:

```python
@dataclass(frozen=True)
class CorruptExportProgress:
    task_id: str
    operation_id: str
    state: Literal["quarantine_pending", "quarantined", "blocked"]
    pages_written: int
    links_exported: int
    complete: bool
    code: str | None
```

Add `quarantine_corrupt(self, task_id: str, *, reason: str, owner: OwnerLease, deadline: float = float("inf"), cancelled: Callable[[], bool] | None = None) -> CorruptExportProgress`.

Acquire queue-operator projection, task fence, then a validated intent fence when the active binding selects an intent. In the first `BEGIN IMMEDIATE`, hash bounded raw payload without parsing it, canonical attempt-history export, and task metadata; capture `lineage_generation`; insert the operation with empty cursor, zero counts, and `rolling_root=EMPTY_SHA256`; move task to `quarantine_pending`; verify one affected row. Operation/disposition key hashes task ID, all three observed digests, actor identity, and reason.

- [ ] **Step 5: Publish fixed package files and bounded lineage pages**

Package layout:

```text
run/queue-results/corrupt-<disposition-key>/
  payload.bin
  attempt-history.json
  task-metadata.json
  lineage-page-00000001.json
  lineage-page-00000002.json
  manifest.json
  disposition.json
```

Publish owner-only create-only files with checked metadata durability. Page incoming links by stable child task ID using `WHERE redrive_of = ? AND id > ? ORDER BY id`; never persist hidden rowid. Stop on the first of 1,000 links, 1 MiB canonical page bytes, 5 seconds, caller deadline, or cancellation. Each page includes previous root and page digest; rolling root is `sha256(bytes.fromhex(previous_root) + bytes.fromhex(page_sha256))`. Insert the page row and advance `cursor_task_id`, root, and count in one queue transaction after read-back verification.

- [ ] **Step 6: Publish manifest, disposition file, then terminal database state**

After paging reaches a stable end, publish schema-valid manifest binding raw/history/metadata hashes, captured lineage generation, total links, page count, and rolling root. Publish schema-valid disposition file with actor/reason and manifest digest. When an intent fence is required, immediately revalidate its exact canonical owner/fence in the coordinator before opening the final queue transaction. In the final queue `BEGIN IMMEDIATE`, re-stream raw payload and history and revalidate task metadata, active link seal, captured generation/count, task fence, stored intent-fence digest, manifest, and disposition. Insert immutable `corrupt_dispositions`, then transition `quarantine_pending` to `quarantined` in that same queue transaction. Do not claim a cross-database atomic read; task-first acquisition, held leases, positive-death takeover, and intent-first release preserve the protocol. Do not delete payload, history, or incoming links.

- [ ] **Step 7: Add exact orphan adoption and retained conflict handling**

An exact package whose files/digests match the deterministic operation is adopted. A package at the same digest path with any mismatch gets `orphan_corrupt_package_conflict`, blocks deletion, and is never renamed or removed automatically.

Add repair-only CLI `supersede-corrupt-package <64-hex-package-key> --reason <text>`. It accepts only the exact `run/queue-results/corrupt-<package-key>/` directory and rejects symlinks, reparse points, unknown names, nonregular files, or files over their contract limits. The allowlist is `payload.bin`, `attempt-history.json`, `task-metadata.json`, `manifest.json`, `disposition.json`, `purge-receipt.json`, and eight-digit `lineage-page-` or `purge-page-` JSON names. Acquire canonical `repair` and project it as queue domain role `operator`. Scan sorted names in resumable pages bounded by 1,000 files, 1 MiB canonical metadata, and 5 seconds, hashing file name, size, and content digest into the rolling root. Publish each create-only scan page under `run/queue-results/corrupt-supersession-<supersession-key>/`, then commit its page row/cursor/root. The key hashes package key, initial package identity, actor, and reason.

After a stable end, re-stream the allowlisted tree and require the same directory identity, count, and root. Publish canonical `corrupt-package-supersession/v1` as `disposition.json`, read it back, then insert `corrupt_package_supersessions` and set its operation to `disposed` in one queue transaction. The bounded record binds original package path/key and identity, observed file/page counts and final root, actor identity, reason, and persisted chosen time; the database row binds the read-back record path and digest, while detailed page roots stay in the operation page rows. It never deletes or renames the original package. The package remains retained for 30 days from disposition and can then be accounted for only in an otherwise eligible offline whole-`run/` deletion; ordinary cleanup never removes either directory. Do not overload `quarantine-corrupt`.

- [ ] **Step 8: Run quarantine tests GREEN**

```bash
uv run --locked --no-sync pytest tests/test_queue_v3_corruption.py tests/test_memory_queue_cli.py tests/test_runtime_deletion_contract.py -q
uv run --locked --no-sync ruff check scripts/memory_queue.py tests/test_queue_v3_corruption.py tests/test_memory_queue_cli.py tests/test_runtime_deletion_contract.py
```

Expected: large fan-out exports resume in bounded pages, final state is disposition-last, and hostile IDs never reach names.

- [ ] **Step 9: Optional checkpoint after explicit operator approval**

```bash
git add scripts/memory_queue.py tests/test_queue_v3_corruption.py tests/test_memory_queue_cli.py tests/test_runtime_deletion_contract.py
git commit -m "feat: quarantine corrupt queue tasks"
```

### Task 12: Implement Retained Leaves-First Corrupt Purge

**Files:**
- Modify: `scripts/memory_queue.py`
- Modify: `tests/test_queue_v3_corruption.py`
- Modify: `tests/test_memory_queue_cli.py`
- Modify: `tests/test_runtime_deletion_contract.py`

- [ ] **Step 1: Write failing retention and leaves-first purge tests**

Cover younger-than-30-day disposition, unresolved capture intent, invalid package, changed frozen root, child with incoming descendants, nonterminal child, young child, page crash, generation race, receipt crash, and final delete race.

- `test_quarantined_task_blocks_purge_for_30_days_and_unresolved_intent`: test each condition alone and together; both retention and independent intent authority must pass.
- `test_purge_moves_parent_to_purge_pending_before_deleting_any_link`: kill after the state change and assert normal redrive creation/deletion remains forbidden.
- `test_purge_deletes_only_terminal_retention_eligible_leaves`: parameterize every child blocker, assert the cursor does not advance past it, then resolve it and resume.
- `test_purge_receipt_binds_a_large_page_history_without_enumerating_it`: seed enough immutable page rows that listing their digests would exceed 64 KiB, then assert the receipt stays bounded and binds their exact count and final rolling root.
- `test_purge_receipt_precedes_final_parent_deletion`: kill on both sides of receipt publication and final delete; receipt-with-row resumes and no API path deletes without the receipt.

- [ ] **Step 2: Run focused purge tests and verify RED**

```bash
uv run --locked --no-sync pytest tests/test_queue_v3_corruption.py tests/test_memory_queue_cli.py tests/test_runtime_deletion_contract.py -q
```

Expected: FAIL because `purge_pending` has no protocol.

- [ ] **Step 3: Implement purge start and authorization**

```python
@dataclass(frozen=True)
class CorruptPurgeProgress:
    task_id: str
    operation_id: str
    state: Literal["purge_pending", "purged", "blocked"]
    pages_written: int
    links_deleted: int
    complete: bool
    code: str | None
```

Add `purge_quarantined(self, task_id: str, *, owner: OwnerLease, deadline: float = float("inf"), cancelled: Callable[[], bool] | None = None) -> CorruptPurgeProgress`.

Require canonical repair/queue-operator ownership, exact task fence, valid disposition/package, 30-day retention, and resolved capture intent. Insert the deterministic purge operation with empty cursor, zero pages, and `rolling_root=EMPTY_SHA256`, then move `quarantined` to `purge_pending` in one transaction. Trigger guards allow redrive-link deletion only while the matching purge operation and token are live.

- [ ] **Step 4: Delete bounded eligible leaves and record pages**

In one invocation process at most 1,000 links, 1 MiB metadata, or 5 seconds. A child is a leaf only when it has zero incoming `redrive_of` rows. It is independently eligible only when terminal, outside retention, has no unresolved intent/decision/result authority, and has no live task fence. Under one `BEGIN IMMEDIATE`, select the stable task-ID page, build canonical `purge-page-<8-digit-number>.json` bytes binding child IDs, task/evidence digests, previous root, and before/after generation, then publish and read back that fixed file while the selection lock is held. Insert each child's `corrupt-lineage` authorization, delete task-owned seal/resolutions/link and attempt history in dependency order, delete the task, then delete the authorization row. Insert the page row and atomically advance parent `lineage_generation`, operation expected generation, cursor, page count, and rolling root. Preserve task-fence epoch history and independently retained semantic/terminal evidence. A crash after file publication but before commit leaves an exact orphan page that retry verifies and adopts into the same transaction; a mismatch is a retained blocker. Stop at the first ineligible child without advancing `cursor_task_id`, and report its stable blocker.

- [ ] **Step 5: Publish purge receipt before final deletion**

When incoming count is zero, publish `purge-receipt.json` as `corrupt-purge/v1` in the same digest-named corrupt package, binding operation, task, package, disposition, original frozen root, immutable purge-page count, final rolling root, final generation, and zero-link observation. The bounded receipt never enumerates page rows; immutable `corrupt_purge_pages` rows retain their ordered digests and intermediate roots. Set operation `receipt-published`. The final transaction re-streams parent raw/history digests; validates task fence, operation token, package/disposition/receipt, exact generation, page count/root, and zero incoming links; inserts `corrupt-parent` authorization; deletes the exact task fence plus task-owned seal/resolutions/link and attempt history; deletes the parent task; and removes authorization. It retains task-fence epochs and corrupt operation/disposition/page rows. A restart with receipt and row resumes this transaction.

- [ ] **Step 6: Add CLI command and ordinary-purge exclusions**

Add `purge-corrupt <task-id>` to the redacted CLI. Ordinary purge must exclude `quarantine_pending`, `quarantined`, and `purge_pending` regardless of age. Automatic retention cleanup never calls corrupt purge.

- [ ] **Step 7: Run purge tests GREEN**

```bash
uv run --locked --no-sync pytest tests/test_queue_v3_corruption.py tests/test_memory_queue_cli.py tests/test_runtime_deletion_contract.py -q
uv run --locked --no-sync ruff check scripts/memory_queue.py tests/test_queue_v3_corruption.py tests/test_memory_queue_cli.py tests/test_runtime_deletion_contract.py
```

Expected: purge is retained, leaves-first, bounded, resumable, and receipt-first.

- [ ] **Step 8: Optional checkpoint after explicit operator approval**

```bash
git add scripts/memory_queue.py tests/test_queue_v3_corruption.py tests/test_memory_queue_cli.py tests/test_runtime_deletion_contract.py
git commit -m "feat: purge dispositioned queue lineages"
```

### Task 13: Implement Bounded Compile Batches And Path-Bound Receipts

**Files:**
- Modify: `scripts/compile_cache.py`
- Modify: `scripts/compile_memory.py`
- Modify: `tests/test_compile_cache.py`
- Modify: `tests/test_compile_transactions.py`
- Modify: `tests/test_compile_integration.py`
- Modify: `tests/test_compile_hardening.py`

- [ ] **Step 1: Write failing source-identity, packing, and retry tests**

Add two daily files with identical bytes at distinct logical paths and assert distinct source identities and receipt paths. Add one source that cannot fit the provider budget and assert no provider call and no receipt. Capture draft and critique prompts and assert source content appears once in draft and not in critique.

- `test_source_identity_hashes_logical_path_and_digest`: recompute both canonical identity preimages and prove identical bytes at two paths produce distinct hashes.
- `test_receipt_path_is_v3_source_identity_not_content_digest`: assert the basename is `v3-` plus the 64-hex source identity, not the shared content digest.
- `test_complete_item_packer_runs_before_provider_dispatch`: parameterize unknown count and oversized mandatory source and assert zero calls, transactions, receipts, and state updates.
- `test_critique_receives_operations_and_cited_evidence_not_source_blob`: inspect both final serialized calls, prove critique omits uncited source bytes, and independently exceed its budget.
- `test_oversized_prospective_receipt_fails_before_writer_gate`: return schema-valid but metadata-heavy operations, exceed 1 MiB for one source receipt, and assert no writer acquisition or side effect.
- `test_successful_retry_produces_identical_operation_receipt_paths_and_bytes`: run with different clocks after a simulated commit-before-return crash and compare operation ID, paths, and exact bytes.

- [ ] **Step 2: Run compile tests and verify RED**

```bash
uv run --locked --no-sync pytest tests/test_compile_cache.py tests/test_compile_transactions.py tests/test_compile_integration.py tests/test_compile_hardening.py -q
```

Expected: FAIL because receipts are digest-only and prompts duplicate the full input blob.

- [ ] **Step 3: Extend immutable source and packing descriptors**

```python
@dataclass(frozen=True, order=True)
class SourceOccurrenceBounds:
    first_event_id: str
    last_event_id: str


@dataclass(frozen=True, order=True)
class SourceDescriptor:
    logical_path: str
    byte_length: int
    sha256: str
    occurrence_bounds: SourceOccurrenceBounds | None = None


@dataclass(frozen=True)
class CompilePackingIdentity:
    algorithm: str
    tokenizer_identity: str
    count_source: Literal["tokenizer", "estimated"]
    max_input_tokens: int
    reserved_output_tokens: int
    safety_margin_tokens: int
    measured_input_tokens: int


@dataclass(frozen=True)
class CompileBatch:
    inputs: CompileInputs
    manifest: Sequence[SourceDescriptor]
    manifest_sha256: str
    packing: CompilePackingIdentity
```

Canonical source identity is:

```python
sha256_bytes(canonical_json_bytes([logical_path, sha256]))
```

Occurrence bounds are metadata and do not change source identity or the existing compile-cache source preimage `[logical_path, byte_length, sha256]`. They do participate in `batch_manifest_sha256` and receipt validation. Extract bounds only from stable event IDs already present in daily logs; otherwise use null. Keep the existing Python field name `byte_length`; serialize it as JSON key `byte_size` in receipt source descriptors. Add a round-trip test so the dataclass, action descriptor, receipt body, and schema cannot drift.

- [ ] **Step 4: Add the complete-item batch packer**

Add `pack_compile_batches(inputs: CompileInputs, *, model: str | None, token_adapters: Mapping[str, TokenCounter] | None = None) -> Sequence[CompileBatch]`. Return an immutable tuple sorted in dispatch order.

Use `ContextBudget(model, 32768, 4000, 1024)`. Count the final system/schema/draft serialization with `count_tokens()`. Record tokenizer identity as `adapter:<model>` or `utf8-byte-estimate/v1`; the fallback is exactly the existing conservative one-token-per-UTF-8-byte estimate. Unknown count fails closed. Never split a daily. Optional index/log/page context is added as complete items only after every daily in the batch fits. Retain existing 4 MiB per-source, 32 MiB aggregate, and 2,000-source hard ceilings as outer safety limits.

- [ ] **Step 5: Make draft and critique independently bounded**

Draft receives one packed batch serialization. Critique receives normalized proposed operations plus only the exact cited evidence quote/path/digest records resolved from immutable batch snapshots. It does not receive `_input_blob(inputs)`. Count final critique system/schema/prompt before call; fail the batch without side effects if it exceeds the same input budget. After critique and Python normalization, build every prospective receipt body in memory and enforce the 1 MiB per-receipt limit plus existing operation/evidence count limits before opening the writer gate. An oversized receipt fails the whole batch with no Markdown transaction, receipt, or diagnostic success update.

- [ ] **Step 6: Implement deterministic v3 receipt bytes**

Replace v2 write paths with these exact APIs:

- `compile_source_identity(logical_path: str, source_sha256: str) -> str`
- `compile_receipt_path(source_identity: str) -> Path`, returning `DAILY_DIR / "receipts" / f"v3-{source_identity}.md"` after 64-hex validation
- `parse_compile_receipt_v3(raw_bytes: bytes, *, logical_path: str, source_sha256: str) -> dict[str, object]`
- `read_compile_receipt_v3(logical_path: str, source_sha256: str, coordinator: MarkdownCoordinator, *, path: Path | None = None, vault: Path | None = None) -> dict[str, object] | None`

Build sorted batch manifest and dispositions before operation ID. `compiled` means at least one committed operation has validated evidence from that source. A source with no committed operation/evidence receives `no_durable_content`. Every manifest source gets exactly one disposition. Operation ID is `compile:` plus SHA-256 of canonical `{action_key, batch_manifest_sha256, dispositions}`. Include packing and provider budget.

The v3 `.md` receipt has exact frontmatter keys `type`, `schema_version`, `source_identity`, `status`, `confidence`, and `source_authority`, followed by the existing `# Compile Receipt`, one-sentence summary, and `## Record` fenced canonical JSON body. Values are respectively `compile-receipt`, `compile-receipt/v3`, the computed source identity, `completed`, `high`, and `ai-derived`. The frontmatter and JSON body contain no wall-clock or retry-time field. Parsing requires exact frontmatter/body agreement, canonical body bytes, path identity, full schema validation, committed transaction authority, and exact operation after-hashes.

- [ ] **Step 7: Commit each batch's outputs and all receipts in one transaction**

For every source in the batch, create one receipt at its path. Existing exact bytes and committed operation are idempotent. Existing different bytes are a conflict. A quarantined/unresolved source gets no receipt and remains pending. Pages, index, log, and all batch receipts remain one recoverable Markdown transaction.

- [ ] **Step 8: Add compile ownership propagation**

Change the public signature to `run_pending_compile(*, trigger: str = "manual", deadline: float = float("inf"), cancelled: Callable[[], bool] | None = None, owner: OwnerLease | None = None) -> int`.

Top-level compile uses marker-first `compile` admission for the complete selection, provider, transaction, and diagnostics lifetime. Nested scheduled/MCP calls pass their owner and do not reacquire/release it. Do not hold the Markdown writer projection during provider calls.

- [ ] **Step 9: Run compile tests GREEN**

```bash
uv run --locked --no-sync pytest tests/test_compile_cache.py tests/test_compile_transactions.py tests/test_compile_integration.py tests/test_compile_hardening.py -q
uv run --locked --no-sync ruff check scripts/compile_cache.py scripts/compile_memory.py tests/test_compile_cache.py tests/test_compile_transactions.py tests/test_compile_integration.py tests/test_compile_hardening.py
```

Expected: path identity, deterministic receipts, provider budgets, no duplicated source blob, and compile ownership all pass.

- [ ] **Step 10: Optional checkpoint after explicit operator approval**

```bash
git add scripts/compile_cache.py scripts/compile_memory.py tests/test_compile_cache.py tests/test_compile_transactions.py tests/test_compile_integration.py tests/test_compile_hardening.py
git commit -m "feat: bind compile receipts to source paths"
```

### Task 14: Preserve V2 History Without Granting V2 Authority

**Files:**
- Modify: `scripts/compile_memory.py`
- Modify: `scripts/archive_daily.py`
- Modify: `tests/test_compile_transactions.py`
- Modify: `tests/test_archive_daily_bagit.py`

- [ ] **Step 1: Write failing conservative-selection and archive tests**

- `test_v2_receipt_is_readable_history_but_never_authorizes_archive`: parse it through the explicitly historical reader, then assert archive remains blocked until the exact path/digest has committed v3 authority.
- `test_v2_receipt_alone_never_suppresses_normal_selection`: provide a valid v2 receipt without a path/digest diagnostic and assert the source enters the bounded normal schedule.
- `test_upgrade_does_not_blanket_schedule_unchanged_v2_sources`: seed the exact path/digest in the pre-existing diagnostic mirror, assert normal migration diagnostics suppress the unchanged backlog, and assert the source still lacks archive authority.
- `test_legacy_diagnostic_basename_must_reconstruct_the_exact_flat_daily_path`: parameterize separators, traversal, malformed values, and two logical candidates; ambiguous keys never suppress selection.
- `test_changed_or_explicitly_selected_v2_source_can_gain_v3_authority`: change the digest and separately use `--file`; both compile the exact snapshot under normal budgets.
- `test_archive_receipt_reference_binds_logical_path_digest_and_receipt_hash`: use identical bytes at two paths and prove neither receipt can authorize the other path.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run --locked --no-sync pytest tests/test_compile_transactions.py tests/test_archive_daily_bagit.py -q
```

Expected: FAIL because current selection and archive lookup use only digest.

- [ ] **Step 3: Keep an explicitly named v2 historical reader**

Rename current parsing internally to `parse_compile_receipt_v2()` and `read_compile_receipt_v2()`. Do not rewrite the schema or files. No skip/archive path may call the v2 reader as authority.

- [ ] **Step 4: Implement conservative normal scheduling**

Selection order for each logical path and current digest:

```text
valid committed v3 receipt -> skip with authority
exact path/digest in compiled_daily_hashes diagnostic -> suppress normal migration-only scheduling, no authority
historical v2 receipt without that diagnostic -> ignore for selection and schedule normally
changed digest or no prior diagnostic -> schedule bounded compile
explicit --file -> schedule unless exact v3 authority already exists
explicit bounded repair selection -> schedule only requested paths within normal budgets
```

The selection path never calls `read_compile_receipt_v2()`. The persisted diagnostic mirror currently uses a flat daily basename; accept a legacy key only when it is a valid direct child name and reconstructs exactly to the candidate's normalized `knowledge/daily/<basename>` logical path. Separators, non-direct children, duplicate reconstructions, or malformed values are ignored and therefore scheduled. This concrete compatibility read avoids blanket recompilation without allowing a digest-only v2 receipt or ambiguous basename to authorize automatic skip or archive; do not rewrite unrelated state consumers in this slice.

- [ ] **Step 5: Make archive lookup path-bound**

`DailyArchiver.eligible()` calls `read_compile_receipt_v3(source.relative_to(vault).as_posix(), digest, coordinator)`. The BagIt reference records logical path, content digest, v3 source identity, receipt relative path, and receipt SHA-256. A v2 receipt yields `compile_receipt_v3_missing` and leaves the source hot.

- [ ] **Step 6: Run focused tests GREEN**

```bash
uv run --locked --no-sync pytest tests/test_compile_transactions.py tests/test_archive_daily_bagit.py -q
uv run --locked --no-sync ruff check scripts/compile_memory.py scripts/archive_daily.py tests/test_compile_transactions.py tests/test_archive_daily_bagit.py
```

Expected: an exact path/digest diagnostic avoids blanket migration work, a v2 receipt alone never suppresses selection or authorizes archive, and exact v3 receipts do both.

- [ ] **Step 7: Optional checkpoint after explicit operator approval**

```bash
git add scripts/compile_memory.py scripts/archive_daily.py tests/test_compile_transactions.py tests/test_archive_daily_bagit.py
git commit -m "fix: keep v2 receipts historical only"
```

### Task 15: Protect Doctor Snapshots And Share Repair Validators

**Files:**
- Modify: `scripts/doctor.py`
- Modify: `scripts/installed_memory_repair.py`
- Modify: `tests/test_doctor.py`
- Modify: `tests/test_runtime_deletion_contract.py`
- Modify: `tests/test_reliability_v3_adoption.py`

- [ ] **Step 1: Write failing adoption, owner, and non-permit snapshot tests**

Add tests for incomplete adoption, every canonical role, expired-live, expired-unknown, expired-dead orphan projection, admission racing snapshot acquisition, snapshot deadline, and post-snapshot owner acquisition.

- `test_pre_adoption_doctor_always_blocks_with_legacy_protocol_unquiesced`: assert no snapshot owner is inserted and the result cannot claim protected observation.
- `test_protected_snapshot_acquires_only_when_no_other_owner_exists`: race each role against snapshot admission and prove exactly one side commits; all later admissions block while snapshot ownership is live.
- `test_snapshot_excludes_only_its_exact_token_and_reports_orphan_projections`: seed canonical-only, domain-only, and mismatched-token rows and assert all remain blockers.
- `test_doctor_never_returns_a_durable_deletion_permit`: check pre-adoption, blocked, and quiescent results; `permit` is always false and offline action is always required.
- `test_protected_scan_fails_closed_before_30_second_lease_expiry`: inject monotonic time at 20 seconds, assert a bounded deadline blocker, and prove release uses the exact snapshot token.

- [ ] **Step 2: Run Doctor tests and verify RED**

```bash
uv run --locked --no-sync pytest tests/test_doctor.py tests/test_runtime_deletion_contract.py tests/test_reliability_v3_adoption.py -q
```

Expected: FAIL because current deletion result returns `allowed: true` without admission protection.

- [ ] **Step 3: Replace deletion permit semantics with a protected snapshot**

Return exactly:

```python
{
    "schema_version": "run-deletion-snapshot/v1",
    "quiescent": False,
    "permit": False,
    "offline_action_required": True,
    "blockers": [{"code": "legacy_protocol_unquiesced"}],
}
```

The closed shape always has these five keys. `quiescent` is a boolean, and `blockers` is a deterministically sorted list of unique closed objects containing only string `code`; a protected adopted scan with no blocker returns `quiescent=True` and an empty list.

Before complete adoption, return `quiescent=False` and blocker `legacy_protocol_unquiesced` without claiming protected admission. After adoption, acquire `runtime-deletion-check` in one canonical transaction that finds no other owner. Hold it while reading both v3 databases and bounded filesystem evidence. Exclude only its exact role/scope/token/epoch. Release before returning. A result is an observation that expires immediately; no command in this repair deletes `run/`.

- [ ] **Step 4: Validate every v3 retained-state blocker**

Doctor checks:

```text
partial/mismatched adoption, tombstone, retired database, or candidate
every canonical owner and orphan domain projection
all nine queue states and every task payload hash/canonical form
capture links, resolution leaves, seals, semantic decisions, task fences
corrupt export/purge operations and orphan packages
all transaction states including aborting/aborted receipt agreement
intent fences and capture binding projections
compile/maintenance compatibility markers
queue results/quarantine and transaction undo retention
```

Retired databases remain blockers. A fully valid tombstone/adoption set does not independently block otherwise eligible whole-run deletion. Unknown liveness and truncated scans fail closed.

- [ ] **Step 5: Make repair inspection and Doctor call the same validators**

Move reusable read-only checks into public functions in `installed_memory_repair.py` or dependency-injected helpers imported by both modules. `inspect_installed_vault()` must not call mutating Doctor mode or acquire an owner. `repair_installed_vault()` after adoption acquires canonical `repair`; adoption itself uses only the separately confirmed offline gate.

- [ ] **Step 6: Restrict automatic repairs**

Automatic Doctor repair may resume prepared operations already authorized by durable evidence. It may not initiate ownership adoption, operator discard, corrupt quarantine/disposition, corrupt purge, terminal capture disposition, or whole-run deletion. Clearing an orphan owner/projection requires lease expiry and positive process-death proof.

- [ ] **Step 7: Run Doctor and repair tests GREEN**

```bash
uv run --locked --no-sync pytest tests/test_doctor.py tests/test_runtime_deletion_contract.py tests/test_reliability_v3_adoption.py -q
uv run --locked --no-sync ruff check scripts/doctor.py scripts/installed_memory_repair.py tests/test_doctor.py tests/test_runtime_deletion_contract.py tests/test_reliability_v3_adoption.py
```

Expected: Doctor reports a protected quiescent observation only after adoption and never a permit.

- [ ] **Step 8: Optional checkpoint after explicit operator approval**

```bash
git add scripts/doctor.py scripts/installed_memory_repair.py tests/test_doctor.py tests/test_runtime_deletion_contract.py tests/test_reliability_v3_adoption.py
git commit -m "fix: protect doctor runtime snapshots"
```

### Task 16: Propagate Ownership Through Queue, Compile, MCP, And Scheduled Callers

**Files:**
- Modify: `scripts/memory_queue.py`
- Modify: `scripts/compile_memory.py`
- Modify: `scripts/mcp_server.py`
- Modify: `scripts/maybe_compile.py`
- Modify: `scripts/scheduled_nightly.py`
- Modify: `scripts/scheduled_weekly.py`
- Modify: `scripts/doctor.py`
- Modify: `tests/test_memory_queue_cli.py`
- Modify: `tests/test_compile_integration.py`
- Modify: `tests/test_mcp_server.py`
- Modify: `tests/test_maybe_compile.py`
- Modify: `tests/test_scheduled_nightly.py`
- Modify: `tests/test_scheduled_weekly.py`

- [ ] **Step 1: Write failing complete-lifetime ownership tests**

Use barriers rather than sleeps. Pause each actor while idle or in provider/child work and run Doctor concurrently. Assert the canonical owner and domain projection remain visible until the actor's terminal return.

- `test_idle_queue_worker_remains_a_deletion_blocker`: pause before first claim and during final idle wait and assert the same canonical/projection pair blocks both times.
- `test_compile_owner_covers_provider_and_transaction_phases`: pause source selection, provider, writer transaction, and diagnostics and compare one marker identity/token/epoch throughout.
- `test_mcp_in_process_compile_receives_caller_owner`: make the MCP handler acquire one marker-backed canonical `compile` lease, instrument registry calls, and prove nested compile verifies but neither reacquires nor releases that lease.
- `test_scheduled_outer_owner_survives_every_nested_phase_and_failure`: parameterize each child failure, prove the outer lease remains through terminal child handling, and assert no child stays nonterminal after release.

- [ ] **Step 2: Run integration tests and verify RED**

```bash
uv run --locked --no-sync pytest tests/test_memory_queue_cli.py tests/test_compile_integration.py tests/test_mcp_server.py tests/test_maybe_compile.py tests/test_scheduled_nightly.py tests/test_scheduled_weekly.py -q
```

Expected: FAIL because current workers, MCP compile, and scheduled subprocess windows are not one ownership lifetime.

- [ ] **Step 3: Hold worker ownership outside the work loop**

`run_worker()` acquires or projects queue ownership before the first claim and releases after final idle wait, child cleanup, and summary. `claim()` receives the live owner and creates task fence with the lease. Worker heartbeat covers canonical owner, queue projection, task lease, and task fence. Loss of any layer prevents terminal transition.

- [ ] **Step 4: Make compile ownership one top-level context**

Direct `compile_memory.main()` and `run_pending_compile(owner=None)` acquire marker/canonical ownership before source selection and release after state diagnostics. Remove independent best-effort lock deletion paths that can delete malformed or dead-looking markers without exact identity proof. Keep the old reader as compatibility evidence.

- [ ] **Step 5: Propagate existing owners in-process**

The MCP compile handler acquires one marker-backed canonical `compile` owner around the in-process call. Doctor bounded maintenance, nightly, and weekly use their existing owner. These callers pass `OwnerLease` to queue, compile, writer, archive, and generation functions. A nested function verifies the allowed parent role and exact live lease before each external publication and does not call registry acquire/release. When a true subprocess remains necessary, the parent waits for terminal exit and the child acquires its own role; no token is treated as transferable to a different process identity.

- [ ] **Step 6: Run integration tests GREEN**

```bash
uv run --locked --no-sync pytest tests/test_memory_queue_cli.py tests/test_compile_integration.py tests/test_mcp_server.py tests/test_maybe_compile.py tests/test_scheduled_nightly.py tests/test_scheduled_weekly.py -q
uv run --locked --no-sync ruff check scripts/memory_queue.py scripts/compile_memory.py scripts/mcp_server.py scripts/maybe_compile.py scripts/scheduled_nightly.py scripts/scheduled_weekly.py scripts/doctor.py tests/test_memory_queue_cli.py tests/test_compile_integration.py tests/test_mcp_server.py tests/test_maybe_compile.py tests/test_scheduled_nightly.py tests/test_scheduled_weekly.py
```

Expected: all complete-lifetime and nested-owner tests pass without detached mutations.

- [ ] **Step 7: Optional checkpoint after explicit operator approval**

```bash
git add scripts/memory_queue.py scripts/compile_memory.py scripts/mcp_server.py scripts/maybe_compile.py scripts/scheduled_nightly.py scripts/scheduled_weekly.py scripts/doctor.py tests/test_memory_queue_cli.py tests/test_compile_integration.py tests/test_mcp_server.py tests/test_maybe_compile.py tests/test_scheduled_nightly.py tests/test_scheduled_weekly.py
git commit -m "fix: propagate runtime ownership end to end"
```

### Task 17: Run Cross-Database Crash And Race Qualification

**Files:**
- Modify: `tests/test_reliability_v3_adoption.py`
- Modify: `tests/test_operational_ownership.py`
- Modify: `tests/test_queue_v3_capture_links.py`
- Modify: `tests/test_queue_v3_corruption.py`
- Modify: `tests/test_transaction_abort.py`
- Modify: `tests/test_runtime_deletion_contract.py`

- [ ] **Step 1: Add deterministic killpoint harnesses**

Use subprocess exit 86 and operation-specific environment variables. Enumerate killpoints from the actual `MigrationStatement` names and publication state machines so adding a statement without a test fails the test. Do not use timing sleeps to infer a crash boundary.

- [ ] **Step 2: Qualify pair adoption against every boundary**

For fresh and upgrade paths, kill immediately before/after manifest, retired copy, candidate backup close, every migration statement, prepared tombstone, legacy replacement, active v3 publication, and adoption publication. After each kill, read-only inspection must classify exact state; explicit offline resume must converge or retain a named conflict. Verify retired hashes and tombstone bytes after every convergence.

- [ ] **Step 3: Qualify ownership projection races**

Race all twelve roles against `runtime-deletion-check`. Race top-level and nested project/writer acquisition, queue canonical/projection boundaries, LSP canonical/filesystem boundaries, marker/canonical boundaries, heartbeat/takeover, and release/successor acquisition. Assert every mixed state blocks and stale cleanup cannot affect a successor token.

- [ ] **Step 4: Qualify dual-fence and side-effect races**

Race worker and operator at every task/intent acquisition and release boundary. Kill after binding seal but before coordinator projection, after projection but before transaction prepare, after Markdown commit but before terminal result, and after abort receipt but before `aborted`. Recovery must adopt deterministic evidence before any provider reinvocation.

- [ ] **Step 5: Qualify corrupt export and purge races**

Kill before/after every fixed package file, each export page transaction, manifest, disposition file, disposition row, each package-supersession page/file/row boundary, purge page, purge receipt, and parent delete. Mutate lineage at each legal pre-freeze point and assert full CAS catches it. Mutate a superseded package between every scan/finalization boundary and assert identity/root CAS catches it. Assert bounded restart work never scans or buffers the complete fan-out or package tree.

- [ ] **Step 6: Run the qualification suite GREEN**

```bash
uv run --locked --no-sync pytest tests/test_reliability_v3_adoption.py tests/test_operational_ownership.py tests/test_queue_v3_capture_links.py tests/test_queue_v3_corruption.py tests/test_transaction_abort.py tests/test_runtime_deletion_contract.py -q
```

Expected: every enumerated crash and race converges to valid authority or an explicit retained blocker.

- [ ] **Step 7: Optional checkpoint after explicit operator approval**

```bash
git add tests/test_reliability_v3_adoption.py tests/test_operational_ownership.py tests/test_queue_v3_capture_links.py tests/test_queue_v3_corruption.py tests/test_transaction_abort.py tests/test_runtime_deletion_contract.py
git commit -m "test: qualify reliability v3 crash recovery"
```

### Task 18: Final Verification

**Files:**
- Verify only; change implementation or tests only when a failing command identifies a root cause.

- [ ] **Step 1: Run the complete focused reliability slice**

```bash
uv run --locked --no-sync pytest tests/test_reliable_memory.py tests/test_reliable_memory_schemas.py tests/test_operational_migrations.py tests/test_reliability_v3_adoption.py tests/test_operational_ownership.py tests/test_memory_queue.py tests/test_memory_queue_migration.py tests/test_memory_queue_races.py tests/test_memory_queue_cli.py tests/test_queue_v3_capture_links.py tests/test_queue_v3_corruption.py tests/test_markdown_transaction.py tests/test_markdown_transaction_recovery.py tests/test_transaction_abort.py tests/test_compile_cache.py tests/test_compile_transactions.py tests/test_compile_integration.py tests/test_compile_hardening.py tests/test_archive_daily_bagit.py tests/test_project_journal.py tests/test_lsp_process.py tests/test_install_pyright.py tests/test_maybe_compile.py tests/test_scheduled_nightly.py tests/test_scheduled_weekly.py tests/test_doctor.py tests/test_runtime_deletion_contract.py tests/test_mcp_server.py -q
```

Expected: PASS with no leaked SQLite connections, live child processes, runtime artifacts in the source worktree, or ResourceWarnings.

- [ ] **Step 2: Run structural and static checks**

```bash
uv run --locked --no-sync ruff check scripts tests benchmark
uv run --locked --no-sync python -m compileall -q scripts tests benchmark
uv run --locked --no-sync pytest tests/test_structure.py tests/test_quality_guards.py -q
git diff --check
```

Expected: all commands pass. The structure test confirms only the approved runtime paths and no third operational database.

- [ ] **Step 3: Run the complete Windows suite**

```bash
uv run --locked --no-sync pytest -q
```

Expected: all collected tests pass on the supported Windows/Python environment. No baseline failure is waived because it predates this branch.

- [ ] **Step 4: Run clean Linux and platform CI evidence**

Push only after explicit operator approval. CI must provide minimum Python 3.10, current Python 3.14, clean production dependency, Linux full-suite, Windows full-suite, and macOS confirmation. Do not mark the slice complete from a focused local run alone.

- [ ] **Step 5: Inspect final diff and contract coverage**

```bash
git status --short
git diff --stat
git diff -- scripts tests
```

Confirm all of the following:

- R1-R2, Q1-Q7, M1-M2, and L1-L6 each have a named regression that fails when its implementation is reverted.
- No v2 schema or historical receipt was rewritten.
- `run/queue-v3.sqlite3` and `run/markdown-transactions-v3.sqlite3` are the only active operational databases.
- Both legacy active paths are tombstones after adoption; both retired v2 files remain byte-retained on upgrade.
- No normal startup auto-adopts an installed v2 vault.
- No queue transition trusts an unchecked payload BLOB/hash pair.
- No nonterminal decision occupies the task terminal result slot.
- No task-backed operator path acquires intent fence before task fence.
- No corrupt package or receipt path interpolates an untrusted identifier.
- No owner takeover relies on expiry alone.
- No Doctor result is a durable deletion permit.
- No automatic path deletes `run/`, retired databases, tombstones, compatibility markers, or personal knowledge.

- [ ] **Step 6: Optional final checkpoint after explicit operator approval**

```bash
git add scripts tests
git commit -m "feat: complete reliability v3 queue ownership"
```

## Completion Evidence

The slice is complete only when the focused suite, static checks, complete Windows suite, clean Linux matrix, and macOS CI confirmation are green. The implementation report must list the exact commands and observed outcomes, state whether offline adoption was tested from both fresh and upgraded fixtures, and state that no real installed vault or personal knowledge was modified. No action is required from an operator until they explicitly choose an offline installed-vault adoption window.
