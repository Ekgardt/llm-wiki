# Lifecycle policy shares the compile after-image

Research checked 2026-10-01. A compile update can append a new authoritative claim to the same page whose old claim the contradiction policy supersedes. Both edits belong to one transaction. Rejecting the shared path prevents a valid update; replacing one whole image with the other loses either new text or the lifecycle decision.

Primary sources:
- SQLite isolation: https://www.sqlite.org/isolation.html — own writes are visible within a transaction; separate writers do not expose partial writes.
- PostgreSQL 18 transaction isolation: https://www.postgresql.org/docs/current/transaction-iso.html — own prior writes remain visible in a transaction, while conflicting external writes require refusal/retry.
- RFC 9110, If-Match: https://www.rfc-editor.org/rfc/rfc9110.html#name-if-match — strong preconditions prevent overwriting another writer. This is a concurrency principle; no HTTP dependency is introduced.

Chosen design: retain the existing writer gate and Markdown transaction. Validate assessed identities against the disk ledger, apply only those verified lifecycle transitions to the compiler's pending page, and require identical original page hashes before replacing its pending transaction operation. Unchanged standalone policy callers use the disk page by default. A repeated same transition within one transaction is accepted only when the complete record equals the verified original or verified resulting record. Different identity remains a refusal. Receipt hashes are derived from final pending pages before receipt serialization; otherwise a repeat would reject its own committed result.

Alternatives rejected: suppress overlap (loses writes), publish policy separately (breaks atomicity), broad text merge (unnecessary ambiguity), accept changed external bases (lost updates). No new dependency, schema, runtime path, model choice, numerical limit, or data migration. Keyword-only optional pending pages preserve existing callers.

Regression: update plus supersession of the same page fails with the original overlap guard on pre-fix code for both v2 and v3 receipts. Candidate preserves new text and active claim, supersedes old claim, survives interrupted application/recovery, and replays the same transaction. Concurrent page change and altered claim identity still refuse. The old guard is replaced by a same-snapshot invariant, not disabled. Existing saved receipts remain authoritative and are never rewritten.
