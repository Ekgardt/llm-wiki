# Event envelope values

llm_wiki.event_envelope_values owns construction of the immutable EventEnvelope:
agent identity, source/payload validation, checkpoint signals, recursive redaction,
freeze/thaw, canonical JSON, source occurrence and content/event hashes. Original
constants, function bodies, field order and generated dataclass behavior stay.
It has only the existing secret_redact dependency and no breadcrumb transport,
filesystem, queue, state-root resolution, provider or capture adapter import.

The physical native-frame decoder stays in event_envelope and uses direct aliases
for its value checks. Existing producers keep their old import paths and the same
class object. EventEnvelope.__module__ remains event_envelope for class metadata;
this does not make MappingProxyType payloads pickleable. Old aliases remain until
integration_adapter, capture hooks, breadcrumb worker and external callers migrate.
No arbitrary patching of the old module's private constructor dependencies is
promised. Existing decoder tests still patch the same stdlib JSON module.

Frozen dataclasses alone do not establish deep immutability: the existing recursive
MappingProxyType/tuple representation and independent thaw are retained. Secret
keys are redacted before hashing, key collisions and invalid replacement values
remain refusals. Capture retry time is excluded from event identity; explicit
source occurrence and source event IDs keep their existing roles. No DLP gateway,
new schema, policy value, dependency, runtime location or installation changes.

The complete packet owns the original producer tests in place; those tests import
the value owner directly, while existing integration helper imports retain their
path. Full secret_redact implementation/guide and the structural-redaction history
remain context. Native parsing and physical-evidence history belong with that
reader, not this constructor. Complete original JSON byte snapshots, class alias
and signature checks supplement unchanged tests; native/capture integration is
verified separately and remains part of the whole-project completion obligation.

## Boundary research — 2026-11-04

Sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[pytest integration](https://docs.pytest.org/en/stable/explanation/goodpractices.html),
[Python dataclasses](https://docs.python.org/3.14/library/dataclasses.html).
Keeping construction with physical decoding unnecessarily imports the transport;
qualifying that combined packet with its full physical-protocol history measured
103610 tokens before new documentation/tests. Per-helper splitting fragments one
identity contract. The chosen cohesive value boundary retains one implementation
and full required dependencies. No version change is inferred from online docs.
Actual complexity/shape, Ruff, unchanged ASTs, original tests, isolated imports,
measured CI weights and exact <=100000-token packet reconstruction are required.
Packet size alone does not establish full-cycle LLM savings or project completion.

## Canonical structural contract

The pure construction/identity/immutable-value contract belongs to
[Event envelope values](event-envelope-values.md); physical native-frame
reading stays in `event_envelope`, with direct compatibility aliases for producers.

## Approved annotation compatibility — 2026-11-04

Native Python 3.14 annotations resolve without loading historical facades. Fields,
defaults and pickle paths are retained; raw annotation/signature representation
changes as explicitly approved. Cold-process tests cover ordinary get_type_hints.
