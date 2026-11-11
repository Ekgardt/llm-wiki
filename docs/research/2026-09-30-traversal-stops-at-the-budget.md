# Traversal stops at the caller's budget

Date: 2026-09-30. Scope: removal of three redundant functional bounds; the wider
law 9 audit remains open. No paths, configuration keys or runtime roots change.

Reproductions show that a twelve-module re-export chain loses its valid call
target, a 641-byte literal client path loses its route edge, and a four-hop
closed graph pipeline is refused. The old values (8, 512 and 3) have no measured
basis. A direct expired-deadline re-export walk also completes without checking
the deadline. Evidence: `logs/audit-2026-09-30-continuation-law9-red.txt`.

Primary sources checked today: [Python deque](https://docs.python.org/3/library/collections.html#collections.deque),
[SQLite recursive traversal](https://www.sqlite.org/lang_with.html), and
[NetworkX breadth-first traversal](https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.traversal.breadth_first_search.bfs_edges.html).
These describe iterative queues, duplicate exclusion to stop cycles, and explicit
traversal-depth semantics. They do not justify the old numeric values. The
implementation uses the existing standard-library deque and set, not a new
NetworkX dependency or recursive SQL query; Python 3.10 remains supported.

Choose cycle-aware traversal of the finite captured module graph, checking the
existing caller deadline/cancellation at each queued hop. Existing extraction
source/byte/record budgets continue to apply. Read the literal route from the
already bounded source, retaining existing stored-target bounds. Accept the
caller's graph pipeline within the unchanged input-byte, row and work bounds;
check its deadline even when a frontier is empty. The extractor version changes
so derived generations cannot reuse old missing edges as current evidence.

Raising the old values merely moves the defect. Making them settings creates
unnecessary knobs without a basis. Unbounded recursive traversal risks cycles
and stack exhaustion. The chosen finite iterative walk can do more work for
long valid chains; caller cancellation and deadlines remain effective and
timeouts propagate rather than publishing a partial successful extraction.

Qualification requires the reproduced boundaries, cycles, expired deadlines,
related extraction/query tests, real Lizard and branch-shape checks. Installation
and the subsequent fresh index must be reported separately from passing tests.

Verified on the installed checkout: five regressions failed before the repair;
the related group then passed **140 tests in 35.70 seconds**, including actual
Lizard, two-if/nesting checks and existing query/extraction guards. Ruff passed.
Evidence: `logs/audit-2026-09-30-continuation-law9-green.txt`. This closes these
three redundant bounds, not the remaining limit inventory or the whole audit.
