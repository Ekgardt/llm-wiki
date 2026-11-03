# Process liveness module

Qualified in its existing location on 2026-11-02. `scripts/process_liveness.py`
remains the sole implementation of local PID probes and recorded process identity
comparison. Its production bytes and public interfaces are unchanged. Queue
cleanup now calls its existing `pid_alive` directly instead of passing through
`memory_state._is_pid_alive`.

The later approved queue cleanup extraction moves that queue-specific adapter
to `llm_wiki.queue_process_cleanup`; memory_queue keeps an identical imported
object for its existing consumers. The common process_liveness implementation
and its invalid-input policy remain unchanged.

## Responsibility and safety contract

`process_state` answers alive, dead or unknown for a local PID. `pid_alive` maps
only proven death to False; unsupported or invalid observations remain True.
This local-PID interface does not prove death of an owner recorded elsewhere.
`process_start_identity`, `recorded_process_state` and `owner_alive` handle the
recorded identity and observation scope. Linux identities include host continuity,
boot, PID/time namespaces and start ticks. Incomparable scopes and missing evidence
remain unknown. A same-host verified reboot and same-scope exit/reuse can prove
that the recorded owner is gone. Darwin and Windows use their existing start-time
probes. Callers retain fencing, lease management and resource deletion decisions.

This is a snapshot observation, not a guarantee that a process stays alive after
return. It creates no locks, threads, registry or runtime files. A local PID
check is not a substitute for a recorded-owner identity check.

Queue's existing adapter deliberately has a narrower legacy input contract:
non-positive or non-integer PIDs, including bool, return False. Its validation
stays in the adapter because changing the generic probe would change other
consumers' conservative policy. Unexpected probe/import errors still return True.
The operating-system probe itself is not duplicated.

## Reproduced dependency and correction

The built-in graph at 64bd9a71 shows queue._pid_is_alive calling the private state
adapter; that adapter adds validation and calls process_liveness. It also shows
use by child-tree cleanup and its tests. Blocking only the unrelated state import
caused the queue to report True even when the available process probe reported
dead. Four new checks failed before correction (three direct-dependency cases and
one source guard). All twelve queue contract checks passed after the direct call,
including invalid input and conservative error handling.

The owned process test file also loaded doctor, markdown_transaction and
memory_state in one integration test. Its unchanged AST was moved to the existing
`tests/test_memory_state_permissions.py`, which owns state/lock integration.
All assertions remain. A guard failed on the mixed file before the move and now
rejects executor imports in both the implementation and its own test file.
No source/test files are renamed and no runtime directory is added.

The historical `memory_state._is_pid_alive` adapter remains for compatibility:
pre-change queue code (including the installed version) imports it lazily. No
current production caller remains in the candidate after this change, but that
alone does not prove that old in-memory callers have stopped. Its integration
assertions remain. Removal requires a qualified upgrade/restart of affected
callers and a consumer check; this step does not claim complete legacy retirement.
There is still one operating-system probe implementation.

## Complete maintenance packet

`process-liveness-context.json` enumerates the complete implementation, own test
file and inline data, shared instructions, canonical structure, module design
and relevant process-identity history. Entire files count against the owner's
100,000 o200k_base-token ceiling. The pinned development tokenizer measures it;
this is not a claim about full task API usage.

```
PYTHONPATH=scripts uv run pytest --noconftest tests/test_process_liveness.py -q -s
```

Own tests exercise local state, PID reuse, identity dispatch, uncertainty,
namespace comparability and import boundaries. Negative import examples prove the
static guard; it is not an OS sandbox or a detector of arbitrary dynamic imports.
Queue CLI/tree cleanup, state permissions, ownership namespaces, LSP ownership
and scheduled fences remain integration tests with their existing contexts.
Moving one integration case does not remove or shrink those scenarios.

## Alternatives and references

A direct call plus the existing legacy input policy restores the established
owner boundary. Importing the state adapter keeps unrelated initialization in
process cleanup. Copying OS probes would duplicate security policy. Changing the
generic invalid-PID policy would alter other consumers. Adding a new adapter
module or provider abstraction is unnecessary. No new limits or dependencies
are introduced; Python 3.10 remains the compatibility baseline.

Primary references checked 2026-11-02:
[Python imports](https://docs.python.org/3.10/reference/import.html) explains module
initialization and import failures;
[Linux kill(2)](https://man7.org/linux/man-pages/man2/kill.2.html) distinguishes
signal-0 existence/permission checks and missing-process errors;
[Windows GetExitCodeProcess](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getexitcodeprocess)
documents successful status queries and STILL_ACTIVE. These support preserving
the existing probes, not replacing recorded identity with PID-only authority.
Python's upstream page marks 3.10 unsupported; this change does not revise the
project's support floor. Native Windows/macOS validation is not claimed on Linux.

Historical research in the packet retains its dated checkpoints. Earlier claims
that missing identity falls back to a local PID probe are superseded by the
current conservative recorded-owner code and namespace tests.

Evidence: local logs/queue-probe-boundary-graph.json, logs/queue-probe-red.txt,
logs/queue-probe-green.txt, logs/process-liveness-boundary-red.txt,
logs/process-liveness-boundary-green.txt and logs/process-liveness-preservation.json.
This removes one dependency edge; memory_queue remains larger than 100,000 tokens.

Qualification: 38 own cases passed on Python 3.12 and 3.10. On 3.10, another
125 queue/state/namespace cases passed. The Python 3.12 related run passed
447 cases and skipped one platform case, including real child/grandchild cleanup,
queue races, cancellation, scoped ownership and global complexity/structure/CI
checks. All six packet checks passed (179 passed, 1 skipped). Queue's changed
function has measured CCN 5. The relocated integration test's AST is unchanged.
Ruff passed. This is related qualification; the preceding full 14,844-case run
belongs to the previous production revision and is not presented as a new full run.

## Existing process-liveness boundary (qualified 2026-11-02)

`scripts/process_liveness.py` remains the stdlib-only owner of local process
probes and recorded start-identity comparison. Own tests remain in
`tests/test_process_liveness.py`; the unchanged three-lock integration case now
lives in the existing `tests/test_memory_state_permissions.py`. Queue cleanup
calls the common probe directly, preserving its legacy invalid-PID handling and
conservative error result. Identity-bearing ownership decisions keep their
existing readers. See `docs/modules/process-liveness.md` and its full context
manifest. No source path, runtime location or OS probe implementation changes.
