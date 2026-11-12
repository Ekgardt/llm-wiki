# Process-local evidence reader leases

`scripts/evidence_reader_cache.py` remains byte-identical at its existing path.
It owns the in-process registry, entry locks, leases, identity-based reuse,
memoized generation values and retirement. It neither selects nor validates a
generation and has no persistent state. The caller supplies scope resolution and
the real validated reader opener. `code_graph` retains those callbacks, deadlines,
single-thread SQLite fallback and generation selection.

## Existing contract

The key is the caller's checkout identity and read-only flag. Reuse checks stat
identities for catalog, artifact and Git state. A changed Git state resolves the
scope again and retains the entry only for the same repository. This is not a
replacement for digest validation on opening, protection against hostile metadata
preservation, or a new hard deadline on lock acquisition.

A lease holds an entry's reentrant lock and must be used and closed on the same
thread. Closing it releases the lease; it does not close a retained reader.
Retirement closes an unheld reader immediately and a held reader after its last
release. Explicit forget/clear, idle retirement and LRU eviction use that policy.
The existing eight-entry and ten-minute thresholds are unchanged, including their
historical rationale. Idle retirement happens on access, with no timer or daemon.
An opener returning None is not cached; opener errors propagate. Existing cleanup
error handling and finalizer behavior remain unchanged.

The adapter rejects SQLite's single-thread build. The shared graph reader sets
check_same_thread=False and the lease lock serializes access to its connection.
The lease boundary does not make arbitrary use of a leaked raw connection safe.

## Qualification — 2026-11-03

The approved autonomous plan supports retaining an already cohesive module.
Combining this cache with graph construction or catalog validation would broaden
its responsibility; adding a new protocol framework has no demonstrated benefit.
No package, runtime location or database format changes.

Sources checked on this date: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[import contracts](https://import-linter.readthedocs.io/en/stable/contract_types/),
[PEP 544](https://peps.python.org/pep-0544/),
[SQLite threading](https://sqlite.org/threadsafe.html),
[Git stat-identity caveats](https://git-scm.com/docs/racy-git) and
[Python 3.14 reentrant locks](https://docs.python.org/3.14/library/threading.html#rlock-objects).
These support explicit ownership and serial access, not a proof of all races.

Five existing cache-policy tests move with unchanged ASTs and decorators; actual
code_graph integration tests remain in their original file and continue running.
New owner tests use actual SQLite connections to verify lifetime and reuse.
Both September 10 design records remain complete historical context, including
their former Python floor and measured performance. No new latency or paired LLM task-cost improvement is claimed.
Full source, owner tests and instructions must fit 100000 o200k_base tokens and
reconstruct exactly; integration tests belong to their own owners.
