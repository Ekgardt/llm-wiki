# Planning does not own another process's writer gate

Date: 2026-09-29. Installed Python 3.14.6; supported project Python versions
remain unchanged. No dependency, schema, path, or runtime-location change.

A live compile published several batches, then refused the next one with
`external LLM work is forbidden during persisted writer ownership`, and resumed
later independent batches. The guard queried whether *any* process had the
persisted writer row, although the invariant is that the caller must not hold a
writer gate while waiting for external work. Another process can acquire the gate
after that observation anyway, so the old check did not establish global exclusion
for a model call. It caused random source failures during ordinary concurrent
capture and project writes.

The graph connects the check to `resolve_compile_plan`, then draft, critique,
validation, cache, and publication. Publication separately acquires the writer gate
and checks source/target/claim-tree preconditions. Those boundaries are unchanged.
A two-process regression holds a real writer gate in a child and resolves a draft
and critique in the parent. Old code rejects this scenario. A separate regression
keeps the same-process persisted-owner check across two coordinator instances;
the existing direct-held-gate test remains in force.

The query now scopes persisted ownership to the current process. This is still
conservative for another thread in that process, and PID reuse can still cause a
refusal; neither is treated as permission to work under a potentially owned gate.
Database errors continue to fail closed. No gate is released on somebody else's
behalf and no publication precondition is weakened.

Primary sources checked on 2026-09-29:

- [Python thread-local data](https://docs.python.org/3/library/threading.html#thread-local-data)
  distinguishes each thread's local state; one coordinator's local flag cannot
  prove absence of another coordinator's persisted ownership.
- [Linux getpid(2)](https://man7.org/linux/man-pages/man2/getpid.2.html) describes
  process identity, including its relationship to threads. Python's portable
  `os.getpid()` already supplies that identity to this writer registry.
- [SQLite isolation](https://www.sqlite.org/isolation.html) describes isolation
  between database connections. A snapshot observation of another writer does not
  confer ownership or prevent a future writer; write serialization belongs at
  publication, where the existing gate and preconditions remain mandatory.

Alternatives considered: removing the persisted check loses the cross-coordinator
same-process guard; waiting for every unrelated writer introduces unnecessary
coupling and still has a check/acquire race; taking the writer gate during model
work violates the existing invariant and blocks capture. Checking the actual
process owner fixes the mismatch without another synchronization mechanism.
