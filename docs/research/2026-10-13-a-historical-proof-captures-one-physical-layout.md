# A historical proof captures one physical layout per logical source

Date: 2026-10-13. Scope: one `_CurrentHistoricalSourceWork` instance.

A live transaction-health stack showed historical source verification as
active work. A separate real diagnostic returned after 596.71 seconds with
`transaction snapshot changed during filesystem inspection`: this was an
optimistic snapshot refusal while a writer was live, not expiration of its
5016-second budget and not evidence of corrupt transaction data. Its
unresolved-quarantine count cannot qualify the entire refused snapshot.

Source inspection found that multiple complete units from one physical day
caused that same day to be captured and parsed repeatedly inside one proof.
The original regression creates two genuine complete units and their committed
v4 evidence, then observes the canonical frame parser. It fails because one
physical layout is parsed twice. The corrected proof captures it once.

The proof retains an immutable tuple of parts for that logical source and
its original bytes. Reuse lasts only for that proof object. The existing
final comparison re-reads every physical daily source and requires exact byte
equality; selected native units and their linked source evidence are checked
again. Receipt binding, complete batch matching, live output hashes, current
receipt re-reading, uniqueness of selected bytes and deadlines remain. A
second proof performs its own capture. Reuse still checks the active deadline
before returning. No accepted outcome is cached between proofs or calls.

Existing append/change-between-captures and change-after-witness regressions
remain, including source, output, receipt, complete-native-companion and
missing-evidence cases. The first related run passed 85 tests; the three new
cases passed. Complexity and final CI qualification remain required.

A real paired single-unit proof kept the same receipt digest, source digest
and successful outcome. Baseline 13.9675 seconds / 311688215 cumulative read
bytes; candidate 13.9352 seconds / 310869787. Both read the daily source twice.
This is no meaningful speedup: the case contains only one physical unit, so
the new intra-proof layout reuse is not exercised. The candidate also includes
the separately qualified receipt-authority projection. The live diagnostic's
large aggregate read count is not exclusively attributed to this change.
Another multi-source probe refused incomplete historical-to-current matching;
that refusal is retained and is not a successful paired measurement.

Alternatives: a persistent accepted-proof cache risks hiding missing or changed
sources; skipping final comparison weakens evidence; re-parsing one unchanged
layout for every unit adds work without a new observation guarantee. The
chosen scope shares only first-view immutable parsing, and retains fresh final
validation. Retaining the part tuple costs references and retained part data
until this proof completes; it avoids separate duplicate layouts for its units.

Primary sources consulted:
- Python bytes immutability, using the project's Python 3.10 API contract:
  https://docs.python.org/3.10/library/stdtypes.html#bytes-objects
- SQLite isolation and a coherent transaction view:
  https://www.sqlite.org/isolation.html
- PostgreSQL 18 concurrency-control and consistent-view concepts:
  https://www.postgresql.org/docs/current/mvcc.html

The database references explain consistent-view alternatives; this does not
add either a database dependency or database isolation to Markdown files.
Python 3.10 syntax compatibility is retained; its vendor documentation now
labels that release unsupported. No interpreter upgrade is part of this change.
