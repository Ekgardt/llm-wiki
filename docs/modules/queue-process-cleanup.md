# Queue process cleanup

Approved 2026-11-02. `scripts/llm_wiki/queue_process_cleanup.py` owns the existing
queue child-tree discovery, termination escalation and verified cleanup. It
depends only on stdlib, `queue_contracts.QueueOperationError` and the common
`process_liveness.pid_alive` probe. It does not launch jobs or own their leases.

The later approved queue_child_runtime extraction makes that module the direct
owner of `ChildRun` and handshake calls into cleanup. queue_core retains the
required aliases used by existing consumers; the cleanup implementation stays
unchanged. Launch orchestration is described in queue-child-runtime.md.

## Contract

`tracked_descendant_pids(pid, platform_name)` returns the known descendants or
None when the Windows snapshot cannot be verified. `terminate_processor_child`
accepts the existing multiprocessing Process, optional platform, cleanup deadline
duration and tracked set. Omitted tracking uses the unique module sentinel;
explicit None remains unknown. Success returns the verified tracked set; failure
raises the existing QueueOperationError with its unchanged code and details.
The process object's creating parent owns calls to this interface.

Snapshot, signals, fallback termination, joins and polling retain their previous
order and values. A live descendant, an unknown tree or an unverified POSIX kill
does not become success. `pid_is_alive` retains the queue's invalid-PID policy
and conservative exception behavior while delegating the OS probe to its owner.
This remains trusted-local-process cleanup, not an OS sandbox or an atomic
observation of every process. Existing platform limitations remain.

The executor keeps process launch, Pipe handshake/result handling, heartbeat,
queue transitions and failure reporting. Its `terminate_processor_child` and
`tracked_descendant_pids` names import the same objects used by `ChildRun` and
the handshake. `pid_is_alive` and `taskkill` are also direct aliases because
the existing CLI and real-grandchild integration tests use them. There are no
wrappers or duplicated definitions. Internal patch points move to this owner;
changing an old executor attribute is not forwarded into another module's globals.
Retire unused aliases only after checking actual consumers, including old live
processes; installed code has not been upgraded by this extraction.

## Tests and complete context

`tests/modules/queue_process_cleanup/` owns cleanup outcome, snapshot, hard-kill,
import-boundary and token-budget tests. Twenty-two existing test/helper/class
definitions were moved with identical AST after renaming their module reference.
All assertions and platform conditions remain. Native Windows snapshot coverage
is skipped on Linux; simulated Win32 outcomes do not qualify the native platform.

Run the independent packet with:

```sh
PYTHONPATH=scripts uv run pytest --noconftest tests/modules/queue_process_cleanup -q -s
```

Its manifest includes entire source and test files, necessary dependency source
and guides, shared instructions, structure and design history. The test counts
o200k_base tokens using the pinned development tokenizer, against the user's
100,000-token ceiling. It does not measure total API task cost. Real worker,
spawn, grandchild, cancellation and failure-blocking scenarios retain their
integration owners and must be run in addition to this packet.

## Evidence and limits

The original queue file held 135,175 tokens; selected definitions held 3,245
before surrounding comments/imports. A small extraction cannot make the entire
queue or project comply with the ceiling. The boundary guard first failed on
the owned tests' executor imports. After extraction, it forbids those imports;
a fresh interpreter also imports and exercises cleanup while refusing the queue,
state and Markdown transaction modules. The guard is not a dynamic sandbox.

All 30 production function ASTs and all three constants match 81d4c01c. The
existing algorithms are relocated, not rewritten. The broader design, current
primary references, alternatives and preservation gates are recorded in
`docs/research/2026-11-02-queue-process-cleanup-boundary.md`. Local evidence lives
in logs/queue-cleanup-preservation.json and logs/queue-cleanup-*.txt. Validation
results are recorded after execution; neither installation nor native Windows/
macOS success follows from Linux tests.

## Queue process cleanup (approved 2026-11-02)

`scripts/llm_wiki/queue_process_cleanup.py` owns the existing child-tree discovery,
termination escalation and verified cleanup algorithm extracted from queue_core:
30 functions and three constants. Dependencies are stdlib, queue_contracts and
process_liveness. Process launch and Pipe protocol belong to queue_child_runtime; heartbeat belongs
to queue_heartbeat. Queue state and failure reporting remain in queue_core. Required legacy names are identical imported
objects, not wrappers or duplicate implementations. Internal patches in tests
target the actual owner. Existing unknown-tree, permission and live-descendant
refusals, deadlines and platform limitations remain unchanged.
Own tests live in `tests/modules/queue_process_cleanup/`; the complete context
manifest and guide are `docs/modules/queue-process-cleanup-context.json` and
`docs/modules/queue-process-cleanup.md`. The packet is bounded by the user's
100,000-token ceiling; worker/spawn scenarios remain additional qualification.
No runtime root, database, dependency or daemon is added. Approval is recorded
in `knowledge/notes/queue-process-cleanup-decision-20261102.md`.
