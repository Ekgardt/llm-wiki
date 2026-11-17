# Transaction inventory follows the inspection deadline

Research date: 2026-10-05. This is a candidate correction, pending installation and operational qualification.

The installed vault has about 140,000 transaction rows and about 40,000 retained undo directories. Doctor stops directory enumeration after 10,000 entries even when its existing time budget remains. A measured read-only inspection took 22.75 seconds and refused a transaction snapshot changed during filesystem inspection. Directory containment checks repeated for retained undo rows accounted for much of the filesystem time. This is evidence of incomplete and expensive inspection, not evidence of database corruption.

The selected first correction streams transaction directory entries under the existing caller deadline and retains only validated transaction identifiers. Each entry still goes through the existing non-following type and containment checks. Unsafe entries, interrupted enumeration and elapsed deadlines remain incomplete. A missing directory retains its existing meaning. No new setting, runtime location or numeric resource ceiling is added.

Relevant primary sources checked on the research date:

- Python 3.10 `os.scandir`: https://docs.python.org/3.10/library/os.html#os.scandir. Directory iteration supports streaming; cached DirEntry metadata is not used as persistent authority.
- SQLite isolation: https://www.sqlite.org/isolation.html. The existing short SQL snapshot closes before filesystem work. Exact subsequent row validation remains mandatory.
- Git's racy metadata documentation: https://git-scm.com/docs/racy-git. Metadata alone cannot establish unchanged content; this correction adds no persistent filesystem-verdict cache.

Alternatives: increasing the hidden entry ceiling would retain the same failure at a larger vault size. Holding a SQLite read transaction across filesystem inspection would obstruct ownership heartbeats on the supported rollback-journal runtime. Suppressing the snapshot-change refusal would claim an unverified state. All three are rejected. Streaming identifier retention still consumes memory proportional to the actual directory inventory, and active concurrent writers may legitimately prevent a verified quiescent health result. Doctor never grants a durable deletion permit.

Evidence: installed source SHA 3945e47a50e227b167d9c024249e8512fe318e2b876d1be516bce808969d8dae; fresh built-in navigation and measured transaction inspection are retained privately in `logs/audit-2026-10-05-step7-transaction-inspection-native-navigation.json` and `logs/audit-2026-10-05-step7-transaction-health-profile.json`.

A complete, freshly validated identifier inventory is reused for undo-directory membership within the same inspection. Incomplete inventory retains the original fresh filesystem check. This is local reuse of a single inspection, never a persistent verdict cache. Exact final SQL revalidation and all retention guards remain unchanged.
