# The next stage: reliability parameters and lease renewal

2026-11-02. Based on add477d3. The user explicitly approved both steps with the answer
"Yes, implement both steps". The decision and STRUCTURE were recorded before the production changes.

## Two sequential moves

1. `scripts/llm_wiki/reliability_defaults.py` becomes the owner of the existing
   frozen dataclass ReliableMemoryDefaults (21 fields) and of the single DEFAULTS.
   Values, types, field order, frozen behaviour and the constructor are kept.
   reliable_memory re-exports the same objects; the historical
   `ReliableMemoryDefaults.__module__ = 'reliable_memory'` keeps the pickle ABI.
   An isolated import/construct does not need reliable_memory, but pickle may
   load the old facade. Trusted old samples are pinned before the move.
   The twelve direct production from-import consumers found are switched
   to the owner in the same lexical scope; compatible names stay in the old API.
2. `scripts/llm_wiki/queue_heartbeat.py` receives the existing _SourceFenceHeartbeat,
   _LeaseHeartbeat, _join_heartbeat_or_refuse and HEARTBEAT_STOP_FLOOR_SECONDS.
   Dependencies: stdlib, the new reliability_defaults, queue_contracts and the existing
   lease_renewal. The MemoryQueue annotation is replaced by narrow typing.Protocol types:
   a lease needs heartbeat and _heartbeat_wait; a source fence needs
   heartbeat_source_fence and _heartbeat_wait. These describe the existing interface;
   they are not wrappers, runtime checks, subclasses or a callbacks registry.

Each module has its own tests/modules/reliability_defaults/ and
tests/modules/queue_heartbeat/, and a guide and manifest in docs/modules/ under the matching
hyphenated name. The full package of each must be ≤100000 o200k_base tokens.
Both moves are qualified sequentially; a failure of the first is not hidden by
carrying out the second. Approval of this plan permits both named steps.

## Why this order

The reliable_memory source is 14069 tokens. The defaults dataclass itself is 232 tokens
without decorators/comments/tests. The heartbeat definitions are 1066 tokens without
the constant/glue. These numbers are not full packages. A plain move of
heartbeat that imports DEFAULTS from reliable_memory leaves a dependency on the
shared durability/IO module and its dependencies for the sake of a small set of values.
Extracting the already existing immutable contract removes this cause without
copying numbers or creating a second set of settings.

Direct consumers of DEFAULTS found: archive_sessions, flush_memory, archive_daily,
repair_exhausted_queue_tasks, installed_memory_repair, operational_ownership,
integration_adapter, scheduled_weekly, compile_progress_migration,
markdown_transaction, memory_queue, private_vault_backup. The AST inventory
is supplemented by a search for qualified/aliased usages before the move; the absence
of a from-import does not by itself prove the absence of a consumer.
A qualified access reliable_memory.DEFAULTS was also found in doctor.py; it is part
of the consumer migration, and the other doctor operations stay with their previous owners.

The queue keeps transactions, fencing checks, the renew SQL and _heartbeat_wait.
The new heartbeat only owns the existing threads, stop Event, lock and error.
The old memory_queue names stay direct aliases where calls are confirmed.
Checks that change the internal stop floor patch the new owner; no redirection of
globals is added. Class identity is checked for the current monkeypatch
consumers of the compile controller.

## Preserving behaviour

Do not change the DEFAULTS values, default arguments, held_since, renewal schedule,
attempt_seconds, error types/codes, locks, thread names or daemon flags.
Keep join followed by is_alive, and the heartbeat_stop_timeout refusal. Do not declare busy SQLite
a lease loss earlier than the existing deadline. Heartbeat success does not grant the
right to bypass fencing when a task completes. A stop failure keeps blocking
the normal completion path; source refresh stores and passes on the original cause of the error.
Starting a new daemon and changing the runtime, schema, env, CLI or installation are out of scope.

Making copies of parameters in heartbeat, extracting methods by line count or introducing
a universal lease service is not proposed. Keeping the whole reliable_memory import
is technically possible, but it keeps the coupling the user asked to
remove. Moving scheduling to asyncio or replacing the algorithm with a third-party system
is not needed for this local module boundary.

## Research and its limits

Three independent primary sources were checked on 2026-11-02:

- [Python threading](https://docs.python.org/3.10/library/threading.html):
  join returns None and needs is_alive to detect a timeout; Event allows
  signalling a stop. So the existing refusal and synchronization are kept.
  Daemon threads may be cut off at shutdown: the move does not remove this
  existing limitation and does not promise graceful cleanup when the process is killed.
- [SQLite busy timeout](https://www.sqlite.org/c3ref/busy_timeout.html): a busy handler
  may wait for a lock up to its budget. This explains the need to account for
  attempt time in renewal and a bounded join; it gives no reason to change the values.
- [AWS: leader election and leases](https://aws.amazon.com/builders-library/leader-election-in-distributed-systems/):
  local duration and checking the right to perform an action are essential for
  lease safety. This is primary experience from 2019, not a new library and not a recommendation
  to bring a distributed service into a local SQLite system.

[Python pickle](https://docs.python.org/3.10/library/pickle.html) was checked in addition:
the importable class name is part of restoring objects. So
the legacy module name is kept with a single owner of the definition.
Python 3.10 is unsupported upstream, but the current project floor is kept and
checked; the move does not claim an extension of upstream Python support.

The built-in index at add477d3 is current. The graph sees calls of the shared join from both stops,
and of renew_until_stopped from both _run methods and from other components. Class-only queries
showed no dependency edges; that is not proof of isolation. Sources,
annotations, imports and real tests supplement the incomplete static graph.

## Checks before and after

The original set of reliable_memory, queue heartbeat stop, lease renewal and stopping
the child process on lease loss: **97 passed, 3 skipped**, 6.47 s on Linux.
This is a baseline, not a qualification of the move not yet carried out.

After approval: private decision and STRUCTURE before the code; old AST and pickle
samples; red boundary checks; migration without duplication; identical values,
identity and old pickle bytes; isolated imports; real heartbeat threads and
stop timeout, source refresh/error; busy/expiry cases; queue/compile lock handoff,
lease-loss/cancellation, ownership and affected defaults consumers. Python3.10,
Ruff, a real CCN/branch-shape check, CI discovery/weights and both full
token budgets are mandatory. Update the index and delete the replaced bodies, keeping
only the documented compatibility names. Do not replace the live vault with the whole
unfinished candidate branch. Do not claim native platforms or the full suite without
actually running them.

Evidence: logs/queue-renewal-proposal-inventory.json; logs/queue-heartbeat-graph.json;
logs/queue-renewal-callers.json; logs/queue-renewal-baseline.txt;
scripts/reliable_memory.py; scripts/memory_queue.py; tests/test_queue_heartbeat_stop.py.

## First step: defaults

ReliableMemoryDefaults and DEFAULTS were extracted without changing the class AST or the values.
The bodies of 13 consumers matched after excluding imports and replacing the qualified DEFAULTS
in doctor with a direct import from the owner. The rest of the reliable_memory implementation also
matched after excluding the moved nodes/imports. Constructor/frozen semantics,
all fields and the historical protocol-4 bytes of the class/default/custom instances are kept.
The boundary guard first detected the old dependencies, then passed; the module's own import
and instance creation in a fresh interpreter do not load durability/queue/state.

Own packet:18passed, initial size70023tokens/23files. Python3.10 own+
reliable_memory:77passed/3skipped. Extended set:646passed/8skipped,84.48s.
The final size
is recorded in logs/reliability-defaults-related.txt/.xml and the final budget log.
It includes queue, transaction/recovery, ownership, archives, scheduled weekly,
progress migration, backup, capture hooks, heartbeat, complexity, structure and CI.
The three skips on3.10 concern platform-specific cases. Measurement sources:
logs/reliability-defaults-preservation.json; logs/reliability-defaults-own.txt;
logs/reliability-defaults-python310.txt; logs/reliability-defaults-boundary-red.txt;
logs/reliability-defaults-boundary-green.txt.

The second step, heartbeat, was not yet implemented at the time of this entry. The live installation
was not changed; public CI, native Windows/macOS and the full suite are not claimed.

## Second step: heartbeat

Both classes and the bounded join now belong to queue_heartbeat. The AST of the moved
bodies matched bee6aac3 after normalizing two queue annotations; the rest of the queue
matched after excluding imports and the moved nodes. The old class names
are direct aliases. The stop floor is patched at the new owner; the old test
was moved into the module's own package. The boundary check first failed on the previous owner.

13 own checks passed, including a real stop timeout, a fresh-interpreter
start of both threads with runtime imports forbidden, the exact scheduler arguments,
publication of the new record and keeping the original cause of a source fence loss.
Python3.10:136passed, including busy/expiry, child loss and the compile controller.
The first run in the old environment did not build because tiktoken was missing; the successful
run used an isolated environment with the project's locked dev dependencies.

Evidence: logs/queue-heartbeat-preservation.json;
logs/queue-heartbeat-boundary-red.txt; logs/queue-heartbeat-own.txt;
logs/queue-heartbeat-python310.txt; logs/queue-heartbeat-related.txt/.xml;
logs/queue-renewal-final-budget.txt. The static graph was refreshed at bee6aac3,
but remains incomplete; the join callers were confirmed and cross-checked against the sources.
The package includes the dependency sources, shared renewal tests and their documentation;
the size is counted in o200k_base, without subtracting the mandatory context.

The extended set gave387passed and one failure of the global CCN guard: the new
imports check combined two independent checks and exceeded CCN5. It
was split; the production code did not change. The repeated qualification of own+global shape/CCN
is recorded in logs/queue-heartbeat-final.txt/.xml. This is not the full regression suite.

Repeated check:25passed (14 own and 11 global complexity/shape),34.78s.
CI weights/discovery:8passed. Ruff and git diff --check passed. All ten
declared packages fit within100000; the queue source remains129165tokens.
Both approved moves are complete in the candidate; the installed vault was not changed.
