# Compile event values independent of journal storage

Date: 2026-11-03. Internal boundary refinement under the approved completion plan.

compile_call_runtime imports compile_progress only for event validation, artifact
identity, digest/snapshot helpers and exception types. compile_answer_records needs
only two exception types. These are pure value contracts, currently placed beside
SQLite journal reads/writes and deadline handling. Both consumers therefore carry
the journal implementation in their required source closure.

Move the existing event dataclass, three exception classes, value constants and
pure encoding/validation helpers to llm_wiki.compile_event_records. Preserve exact
function/class ASTs, constants, JSON bytes, hashes, messages and class identity.
Keep compatibility re-exports in compile_progress, with historical class __module__
values for pickles. Consumers import the actual value owner directly. SQL schema,
queries, transactions, transition sequencing and database audit stay in storage.
No dynamic import, proxy, source-closure exemption or duplicate implementation.

Before editing: fresh builtin index/callers and source inspection, record old
outputs and protocol-4 pickle bytes, verify consumer attribute sets and existing
monkeypatch sites. Existing tests remain runnable through the old storage facade;
new value tests cover serialization identity and malformed/untrusted metadata.
An import guard must reject journal/queue/controller/SQLite dependencies in the
value owner. Check all moved function ASTs and every untouched storage function.
Retain full necessary contracts/history records, recompute consumer closures and
round-trip every packet; the 100000-token maximum does not change.

Alternatives: keep the unnecessary storage dependency (constrains isolated work),
inject individual scalar validators everywhere (more wiring without a separate
responsibility), or hide storage behind dynamic import (does not remove coupling).
A pure event-value module matches the existing data contract without redesigning
state machines. Compatibility aliases stay because real callers and pickles use
the old path; remove only with an explicit compatibility migration and evidence.

Primary sources checked today (independent publishers):
- Python dataclasses contract, especially frozen fields/asdict and field order:
  https://docs.python.org/3.10/library/dataclasses.html
- Cockburn's original ports/adapters paper separates internal logic from database
  adapters and requires tests that expose boundary leaks:
  https://alistair.cockburn.us/hexagonal-architecture
- Seemann's Composition Root article keeps dependency wiring at entry points:
  https://blog.ploeh.dk/2011/07/28/CompositionRoot/
These are established principles, not measured full-cycle token savings. Native
CI, dependency remediation and whole-project integration ownership remain open.
