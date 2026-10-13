# Repair partitions share one immutable byte proof

On 2026-10-13, the installed compiler's live stack reached
`_repartition_compile_repair` → `_repair_operation_present` → `_critique_prompt`
→ `EvidenceResolver.resolve_bytes` → `_resolve_uncached_bytes` → `daily_entries`.
The compiler was preserving rejected proposals while finding a complete source
partition. The active code generation named commit
`4580a9ba3c4f769f1d6eb080b829f04cf462ee93`; its navigation graph was incomplete,
so the call chain and resolver ownership were also checked in source.

The existing repair scenario reproduced repeated parsing: each of three
unchanged, strongly held source byte objects was parsed twice during one repair.
The new regression failed against the installed implementation. It also checks
that every physical part, reviewed proposal, and verdict survives partitioning.
This proves redundant work in this path, not its share of whole-cycle latency.

## Research and choice

Primary sources checked on 2026-10-13:

- [Python memoization documentation](https://docs.python.org/3.10/library/functools.html)
  describes reuse of pure computations and the retention of cached arguments.
  Its unbounded global-cache alternative would retain whole daily files beyond
  the repair that owns them. The compatibility documentation now marks Python
  3.10 unsupported; this change nevertheless preserves the product's existing
  Python 3.10 contract and introduces no new library feature.
- [SQLite isolation documentation](https://www.sqlite.org/isolation.html)
  distinguishes snapshots from changes made by other connections. A reused
  immutable byte layout cannot replace current-source or transaction checks.
- [Linux process accounting documentation](https://www.kernel.org/doc/html/latest/filesystems/proc.html)
  distinguishes process read accounting from storage reads. Neither cumulative
  counter measures occupied disk space or establishes end-to-end acceleration.

[Python context-variable documentation](https://docs.python.org/3.10/library/contextvars.html)
also confirms token-based restoration of an enclosing context. It is additional
documentation from the first authority, not a fourth independent authority.

The chosen change enters the existing `_source_choice_resolution` context for
the repair's candidate cuts. The existing resolver retains immutable byte
objects and their validated hashes, entry positions, and line positions for
that context. It still validates every referenced span and complete quote.
Nested ownership and context restoration use the existing implementation.
No global cache, second resolver implementation, new limit, configuration,
runtime path, or persistent state is added. Reparsing every proposal was the
baseline; a process-global cache was rejected because of its lifetime and
retention consequences.

## Qualification boundary

The regression requires one parse per immutable layout in one repair and a
fresh proof on a subsequent independent repair. Existing tests must still
refuse spanning proposals that cannot be partitioned completely, ambiguous
claim origins, missing evidence, and unresolved rejected source work.
The running installed compiler is deliberately left on its original commit
until it finishes. This candidate is not an installed correction or a claim
that the full compile cycle is now fast.

Local qualification: 91 related tests passed in 6.35 seconds; 47 structure and
measured-weight checks passed in 13.86 seconds. Ruff passed. Lizard measured
CCN 4 for the changed function and CCN 1 for both new test functions. AST
inspection measured two `if` statements and nesting depth two in the changed
function, zero in the new tests. These are local results, not full platform CI.
