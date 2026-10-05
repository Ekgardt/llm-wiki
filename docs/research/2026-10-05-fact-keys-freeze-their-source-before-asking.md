# Fact keys freeze their source before asking

Date: 2026-10-05.

A genuine full nightly pass failed while collecting sources for fact keys: `CorpusChanged` named the active `knowledge/daily/2026-10-05.md`. Capture was appending while the collector checked its complete snapshot. Retrying under the same active writer does not establish a quiet read.

The collector now takes the existing canonical Markdown writer gate only while it captures immutable source bytes. Admission, waiting and collection share the caller's existing deadline. The gate ends before reading pending keys, calling a provider, writing derived keys or extending entity pages. No runtime directory, database format, tool, provider or setting is added. Source rechecks remain: a non-cooperating editor can still cause a visible collection failure.

Skipping the open day would omit current facts; increasing retries would retain the race; holding the gate while asking a model would delay unrelated writers unnecessarily. Brief exclusion during the existing snapshot operation is the chosen alternative. Durable capture ingress retains events before projection, and projections may wait during collection. This coordinates cooperating writers and is not an operating-system filesystem snapshot.

Three regression tests use real adopted v3 databases and the canonical gate. The old code failed all three. The candidate passes: another coordinator cannot enter during collection, can enter from the controlled test provider after release, and a changed-source exception or busy gate remains a failure. The provider reply in this test is explicitly a fixture, not native model qualification.

Sources checked on 2026-10-05: [SQLite isolation](https://www.sqlite.org/isolation.html), [Python context managers](https://docs.python.org/3.10/library/contextlib.html), [Git racy metadata](https://git-scm.com/docs/racy-git). SQLite isolation applies to database connections; it alone does not freeze Markdown files, which is why the existing common writer protocol is required.

Evidence: `logs/maintenance/20261005T195432-fact-keys-2502507.err.log`, `logs/audit-2026-10-05-step7-fact-key-stable-snapshot-design.json`, `logs/audit-2026-10-05-step7-fact-key-snapshot-original-red.log`, `logs/audit-2026-10-05-step7-fact-key-snapshot-first-green.log`, `logs/audit-2026-10-05-step7-fact-key-snapshot-all-complexity.json`. Point 7 remains open until installation and real operation are qualified.
