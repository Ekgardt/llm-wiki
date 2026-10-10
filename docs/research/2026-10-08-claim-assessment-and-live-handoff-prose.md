# Claim assessment and a concurrently changing handoff

Research date: 2026-10-08; Python 3.10 remains supported. The full installed compile reports persisted claim-tree manifest failures. Inspection of retained attempts finds the generated state of an unrelated active project changing while claim assessment runs. This is a live observation, not yet proof of the only cause at each failure. A deterministic regression changes one unrelated project state after every assessment and before publication; its ledger remains absent throughout.

The claim index consumes validated Markdown claim ledgers, whereas the existing transaction manifest binds every claim-capable page byte. Preserve the complete transaction manifest and every model-input target hash. Record the exact validated ledgers consumed by index rebuilding, and under the writer gate compare them with a fresh stable Markdown snapshot. Only an identical ledger map and unchanged model targets may refresh the full file manifest for publication. Actual ledger changes, membership changes, malformed ledgers, source errors and later file changes remain refusals. No cached database becomes authority, no model call runs under the writer gate, and no schema, path, environment contract or runtime directory is added.

Alternatives: pausing unrelated projects violates the approved concurrent product workflow; removing the transaction precondition weakens safety; endless reassessment repeats model cost under continuous handoff activity; accepting a fresh manifest without comparing the exact index inputs can accept stale assessment. The bounded, semantic comparison retains Markdown authority while the final transaction still compares full file bytes. Compatibility and performance must be qualified separately; this document does not claim the installed defect fixed.

Independent primary references checked on this date:
- SQLite isolation and short serialized writes: https://www.sqlite.org/isolation.html
- PostgreSQL serialization failure handling requires restarting the decision logic when its read dependencies change: https://www.postgresql.org/docs/current/mvcc-serialization-failure-handling.html
- Microsoft Azure architecture explains reducing unnecessary coordination while preserving concurrency correctness: https://learn.microsoft.com/en-us/azure/architecture/guide/design-principles/minimize-coordination

## Local qualification

The original live-handoff regression failed after all existing publication retries with `persisted claim tree manifest precondition failed`. With the change, continuously changing ledger-free project prose commits after one assessment. A real ledger change requires reassessment; a malformed ledger refuses publication; a ledger present only while the index reads it is detected even when the surrounding snapshots return to the original bytes. Existing late tree insertion, continuous tree mutation, stale model targets and same-ID lifecycle replacement checks remain active. Lifecycle conflicts retain the existing candidate-only quarantine before the new comparison.

The related suite passes 172 tests; the targeted Python 3.10 run passes 9. Actual AST and Lizard analysis covers 25 changed or nested callables, with maximum CCN 4 and no violation of the two-if/two-level requirements. Full regression and installed-vault qualification remain pending. These local results do not close the full-cycle task or establish total token savings.
