# The night finishes its entity writes before compile

Research date: 2026-10-05.

The nightly pass spawned compile before running fact-key extraction. Compile
freezes its note targets before asking the model. Fact keys can update those
same notes. The real pass on October 5 committed an entity-page replacement at
22:04:30, while the first compile model call was running. Publication correctly
refused the stale target snapshot. That model work could not be reused against
the changed target.

Run the fact-key step to completion before triggering this pass's compiler.
The subprocess runner already waits for fact keys; the compiler is still
spawned and followed through the existing wait mechanism. Both steps run even
when an earlier step failed. The total budgets, failure handling, source
validation, target CAS, and external-work boundary remain unchanged.

The regression exercises the real compile target guard with physical target
bytes. Original ordering fails after the pass's own entity replacement. New
ordering snapshots the committed entity facts. An independent edit after the
snapshot still fails and requires a fresh model plan. This removes the pass's
own scheduling overlap; it does not promise exclusion of external editors or
other admitted writers, and it does not fix separate invalid model citations.

Holding the global writer during model calls was rejected because it violates
the external-work boundary. Ignoring changed targets would lose concurrent
facts. Blind retries would repeat the same scheduling cause.

Primary sources checked on the research date:

- [Python 3.10 subprocess](https://docs.python.org/3.10/library/subprocess.html):
  synchronous completion versus a separately spawned process.
- [SQLite isolation](https://www.sqlite.org/isolation.html): committed snapshots
  and the need to start a fresh view after conflicting changes. Operational
  databases retain the existing rollback-journal contract.
- [Git update-ref](https://git-scm.com/docs/git-update-ref): compare an expected
  old value before publishing a replacement. The compile target guard keeps
  the analogous precondition; this change adds no Git operation.

Evidence: `tests/test_nightly_fact_keys_precede_compile_snapshot.py`;
`scripts/scheduled_nightly.py::_nightly_steps`;
`scripts/compile_memory.py::_require_current_compile_targets`.
