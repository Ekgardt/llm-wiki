# The next queue slice: stopping the process tree

2026-11-02. The working session of that branch records the user's answer "continue" after a concrete
boundary was proposed (translated from Russian; the repository holds no copy). The decision was recorded before the code; the implementation results are below.
Basis: 81d4c01c, index generation-18e454e27e69b91c-8bb023e5.

## What is proposed

Extract `scripts/llm_wiki/queue_process_cleanup.py`: the existing discovery
of descendants, stopping of the child process and checking that its tree has ended.
Its own tests: `tests/modules/queue_process_cleanup/`. The description and the full
manifest: `docs/modules/queue-process-cleanup.md` and
`docs/modules/queue-process-cleanup-context.json`.

Move, without changing their bodies, 29 functions from `_kill_process_group` to
`_terminate_processor_child`, three constants `_TH32CS_SNAPPROCESS`,
`_MAX_PROCESS_PATH`, `_DISCOVER_DESCENDANTS` and the existing `_pid_is_alive`.
30 functions in total. Names and signatures are kept at first; this is an internal module
of the queue, not a universal service for managing arbitrary processes.

Dependencies: the standard library (including multiprocessing, typing and ctypes),
`llm_wiki.queue_contracts.QueueOperationError` and the existing
`process_liveness.pid_alive`. Imports of the executor, state, capture, diagnostics,
Markdown writers and LLM in the new module are forbidden by checks.

Process start, `_processor_child_entry`, the Pipe protocol, `_ChildRun`, heartbeat,
the store, tasks, reporting and the queue lock stay in memory_queue.
The queue calls the owner's imported functions. Do not add a callback
registry, globals proxies, wrappers or a second copy of the algorithms. Old names
needed by current consumers and integration tests may be direct
aliases of the same objects; each kept name requires a usage check.
A monkeypatch change of the old internal name must not be artificially
redirected: checks patch the actual owner, keeping their assertions.

## Grounds and limitations

memory_queue now takes 135 175 o200k_base tokens. The bodies of the chosen
definitions and constants are 3 245 tokens, without surrounding comments, imports,
tests and documents. This is **not** the size of the future full package and not a promise
to make the whole queue smaller than 100 000 with one move.

The built-in graph confirms the dependency of cleanup on QueueOperationError and
on local check/stop functions. MCP dependencies answered with a timeout because of
busy workers; the same built-in code_graph read the current generation
from a separate process. The graph is incomplete, so the boundary was additionally checked
against the sources and AST/symtable. Name matches in workspace_revision and sync_memory
are their own functions, not consumers of memory_queue functions.
Merging them into this module is not proposed.

Invariants: an unknown Windows snapshot is not equal to an empty tree; a refused signal
is not equal to confirmed termination; a group that vanished after SIGTERM permits
success only if the other checks still hold; a live descendant blocks success.
The handshake order is kept: the tree is read after the task completes, before
the process is stopped. The existing deadlines, intervals and errors are kept.
There are no new limits, runtime paths, dependencies or background services.

The extraction does not strengthen OS containment: it is the same cleanup of trusted local
processes, not a sandbox and not protection against a deliberate process escape. Linux checks
do not prove that native Windows/macOS work. The live installation is not replaced with the whole
unfinished working branch.

## Alternatives and sources

Keeping the whole subprocess runner together with diagnostics means keeping
reverse dependencies on the executor. Moving only the terminate function means
leaving its algorithm scattered between owners. Replacing the algorithm with a single
Process.terminate loses descendants. Introducing psutil or merging all platform
kill helpers now means changing behaviour and dependencies without need.
The chosen option is the narrow existing algorithm with a single owner and a kept
contract; this is a conclusion for this project, not a universally best architecture.

Three independent primary sources were checked on 2026-11-02:

- [Python multiprocessing](https://docs.python.org/3.10/library/multiprocessing.html):
  terminate does not stop descendants; a forced stop may damage
  a pipe/queue. So the start and the result handling stay with the queue, and
  the tree check is kept. The page marks 3.10 as unsupported
  upstream; the project's compatibility floor is not changed by this move.
- [Microsoft Toolhelp](https://learn.microsoft.com/en-us/windows/win32/toolhelp/taking-a-snapshot-and-viewing-processes):
  a snapshot is walked through Process32First/Next, and the handle is closed. The existing
  distinction between the end of the list and an error must be kept.
- [Linux kill(2)](https://man7.org/linux/man-pages/man2/kill.2.html): signal 0
  checks existence and permissions; a successful send to a group does not prove the death
  of all its processes. A separate termination check remains mandatory.

## Implementation order after approval

1. Record the private decision and update STRUCTURE before moving the code.
2. Pin the AST of the definitions, the constants, defaults and the original results of
   the related tests. Prove the original dependency of the unit tests on the executor.
3. Move the algorithm, delete the old bodies, check AST identity and
   the uniqueness of the sentinel. Switch the unit tests to the owner without weakening
   assertions. Keep the real worker/grandchild scenarios as integration tests.
4. Check the import without the executor, negative import guards, snapshot errors,
   permissions, descendants and deadlines. Run real spawn/grandchild,
   timeout/cancellation, task blocking on cleanup failure, Python 3.10,
   Ruff, complexity measurement and structural/CI discovery checks.
5. Measure the full maintenance packet with sources, tests, data,
   instructions, history and the required contracts. Keep within 100 000 tokens
   without hidden exclusion of required context. Count integration scenarios
   separately and run them, without passing them off as independent unit tests.
6. Update the built-in index, check legacy consumers, save the results.
   Do not declare the whole project modular or full-cycle LLM savings without measurements.

Evidence: logs/process-cleanup-graph.json, process-cleanup-closure.json,
process-cleanup-proposal-inventory.json, process-cleanup-baseline.txt;
scripts/memory_queue.py; tests/test_memory_queue_cli.py;
tests/test_memory_queue_races.py; tests/test_already_dead_group_is_not_a_cleanup_failure.py;
tests/test_a_childs_tree_is_read_when_it_is_killed.py.

## The original check before the move

On 81d4c01c, the selected cleanup/descendant/snapshot/tree scenarios in the four
listed test files: **20 passed, 1 skipped, 76 deselected**, 79.27 s,
Python 3.12.3, Linux. The native Windows snapshot check was skipped. This is the original
selected set, not a qualification of the future move and not the full suite. Selection:
`cleanup or descendant or snapshot or tree or pid_probe or already_dead or
refused_signal or delivered_signal or branch_is_skipped`.

## Implementation and qualification

After approval the decision was recorded in the private queue-process-cleanup-decision-20261102,
and STRUCTURE was updated before the move. All 30 function ASTs and three constants matched
81d4c01c. The remaining executor AST matched after excluding imports and the move.
22 definitions of tests/helper objects matched after replacing the name of
the owner module. The original assertions and skip conditions are kept; the old
test_already_dead_group_is_not_a_cleanup_failure.py is now the module's own
tests/modules/queue_process_cleanup/test_group.py, and the cleanup/snapshot unit tests
from CLI/races are in the same package. The integration scenarios stayed in place.

The boundary guard first failed on three files that imported memory_queue, then
passed. A fresh-interpreter test forbids executor/state/writer imports and checks
the probe, tracking, cleanup verdict and sentinel identity. The four required
queue names are direct aliases, which a test checks separately. No other old
bodies or proxies remain. The signal import was removed as no longer used.

The extended Linux set on Python 3.12: **349 passed, 1 skipped**, 150.15 s.
It includes module/queue/CLI/races, real spawn/grandchild, lease loss,
cancellation, startup death, blocked task, liveness, structure, CI and the shared
control of CCN/branching shapes. The new alias identity test passed separately after
collection of this set. Python 3.10: **38 passed, 1 skipped** own and
**77 passed** integration. The Windows snapshot was skipped on Linux. Ruff passed;
the maximum measured CCN of the new production module is 5.

Seven budget packages were checked: 71 passed, 265 deselected. At the time of this
run queue-process-cleanup took 82 670 tokens / 29 files; the final
size after adding this report is measured again. The queue source is
131 955 tokens, the new source 3 346. This is not the completion of the decomposition of the whole
queue and not proof of a reduction in total LLM spending. The measured
CI weights of the moved test files were updated; the weight of the vanished old file was removed.

Evidence: logs/queue-cleanup-preservation.json; logs/queue-cleanup-boundary-red.txt;
logs/queue-cleanup-boundary-green.txt; logs/queue-cleanup-related.txt and .xml;
logs/queue-cleanup-python310-own.txt; logs/queue-cleanup-python310-related.txt;
logs/queue-cleanup-complexity.json; logs/queue-cleanup-budgets.txt.
The full suite of the new HEAD, remote CI, native Windows/macOS and installation are
not claimed here. The previous full run refers to the baseline before this move.
