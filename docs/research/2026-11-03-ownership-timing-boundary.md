# Ownership time values, separate from clocks and registry IO

Date: 2026-11-03. Internal boundary under the approved decomposition mandate.

Transfer _as_utc, _timestamp, _parse_timestamp, _timing,
_require_lease_timing and _LONG_LEASE_ROLES exactly from operational_ownership
into llm_wiki/ownership_timing.py. Old aliases and serialization identities stay.
No current-time reads, database opening, mutation, process probes or heartbeat
execution move. The epoch validator remains with registry validation.

Preserve aware-only acceptance, existing error classes/messages, historical whole-
second reading, six-digit UTC emission and role duration pairs (120/40 or30/10).
These are existing persisted-lease contracts, not new arbitrary limits. A general
ISO reader accepts naive timestamps as UTC; ownership intentionally refuses them.
Combining their policies or making timing configurable during extraction is rejected.
A second registry or injected clock framework adds no value to this value boundary.

Fresh builtin index9e6d432b: generation-18e48fed4d82176f-118c863d. Architecture,
all five helper callers, registry and heartbeat graph queries used no fallback;
direct searches found no patches of these five helper names. Graph is partial,
so direct source and consumers supplement it. Baseline75passed including actual
SQLite expiry, registry, compile and weekly scheduling.

Before transfer, exercise offset normalization, chronological ordering around the
whole-second boundary, naive/invalid refusal and every declared role duration
against the original implementation. Move the original pure timestamp roundtrip
test unchanged; keep real expired-lease registry integration. Verify exact ASTs,
alias identities, isolated imports, real integrations and full packet roundtrip.
Full sources for ISO and ownership value dependencies remain in its context.

Primary publishers checked2026-11-03:
- Python3.14 datetime: https://docs.python.org/3.14/library/datetime.html —
  aware and naive values have distinct semantics; normalization retains the instant.
- SQLite date/time: https://www.sqlite.org/lang_datefunc.html — no dedicated date
  datatype; current project uses text. Fixed-width UTC sorting is verified locally,
  not inferred from SQLite accepting arbitrary ISO spellings.
- Fowler: https://martinfowler.com/bliki/BoundedContext.html — separate responsibilities
  and explicit relationships; the principle alone does not prove time correctness.

No dependency/schema/runtime-root/public command/installation changes. This packet
alone does not qualify the registry's larger composition closure.
