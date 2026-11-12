# Ownership and Markdown transaction value boundaries

Date: 2026-11-03. Proposed internal sequence under the approved mandate.

The ownership registry imports the Markdown coordinator, while a transaction
intent-fence annotation refers back to OwnerLease. Move record definitions before
moving policy or operations: llm_wiki.ownership_contracts will own OwnerRole,
ProcessState, OperationalOwnershipError, ProcessIdentity, OwnerLease and
MarkerIdentity. Its only product dependency is the existing runtime file identity
record. Keep the role sets, timing validation, registry, probes and database
transactions in operational_ownership. Preserve legacy class module names and
facade identities; these values do not themselves prove current lease authority.

Then llm_wiki.markdown_records can own the existing seven public dataclasses,
ChangeKind, the byte-content constructor guard and fourteen public transaction
exception classes. IntentFence's type-only owner reference must resolve through
the new value owner, not the registry. Private staging/OS records and coordinator
execution stay in place. Preserve field order/defaults, frozen behavior, exception
attributes/messages/reduce tuples and legacy pickle names; do not introduce new
validation or substitute a string/type alias for the real lease record.

Alternatives: move the whole registry/coordinator (coupled implementation and
unbounded context); retain a reverse registry dependency merely for an annotation;
remove typing to hide the dependency; duplicate values in both owners. Chosen
sequence separates existing data contracts from side effects and keeps one class
object for every old and new import. Root execution/recovery integration tests
remain in the complete inventory and are not declared covered by value tests.

Qualification: capture pre-transfer field/default/serialization fixtures; compare
exact AST definitions and unchanged remaining functions/constants; test old facade
identity and trusted old pickle loading, current roundtrips and exception state;
verify isolated imports, real ownership/transaction integration, CCN and full
packets. No new runtime path, data format or live deployment. Pickle tests use only
locally generated trusted fixtures; this introduces no untrusted deserialization.

Three independent primary publishers checked today:
- PSF dataclass fields/default/frozen semantics and pickle's importable class
  identity/reduction contracts:
  https://docs.python.org/3.14/library/dataclasses.html
  https://docs.python.org/3.14/library/pickle.html
- Martin Fowler, data-transfer objects and separation of serialization concerns
  (2003, note2013; established guidance, not a new benchmark):
  https://martinfowler.com/eaaCatalog/dataTransferObject.html
- Alistair Cockburn, original ports/adapters boundary and independent testing
  (2005; qualification must detect boundary leaks, not add layers for appearance):
  https://alistair.cockburn.us/hexagonal-architecture
