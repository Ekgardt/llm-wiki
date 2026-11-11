# Model value records

This owner contains ProviderDescriptor, LLMResult, BackendResponse,
CodexExecutable and CodexPlanningBasis. They describe an existing candidate,
result and attempt-owned planning identity. They do not select a provider, run
a process, make a network call, verify a binary or enforce DLP. Those operations
remain in llm_client.

All original dataclass fields, ordering, defaults, positional construction,
comparison exclusions, repr exclusions and shallow frozen semantics are retained.
ProviderDescriptor.canonical emits the same restricted JSON domain and excludes
private endpoint, resolution failure and planning basis. Unknown usage stays
unknown, distinct from zero. Supplied mappings remain shallow: this transfer
adds no deep freezing or validation. The planning descriptor replaces only model
and private basis identity on the original descriptor.

llm_client keeps direct aliases because existing callers, annotations,
isinstance checks and serialized objects depend on that path. Classes retain
__module__ = llm_client. Remove aliases only when those callers and retained
serialized objects no longer require them; there is one class implementation.
The connected planning/executable types live here to make runtime type hints
resolvable without a reverse import. PreparedCodex stays with the transport.

Pure tests cover original fields/signatures, canonical values, default factories,
frozen behavior and type hints. The separate real-client compatibility test
reads pre-transfer synthetic pickle bytes; unchanged provider, DLP and compile
recovery tests remain integration obligations. Full source and dependency guides
are included; no source-closure exemption is added.

Source: ../research/2026-11-03-model-value-record-boundary.md.
Shared canonical contract: [Token values and context budgets](context-budget.md).
Shared canonical contract: [Closed formats](formats.md).

## Structural reference retained from the overview

### Provider value records

`scripts/llm_wiki/model_records.py` owns the five existing provider/result and
planning identity dataclasses, independently of transport, DLP, model selection
and executable verification. `llm_client` retains direct aliases and historical
class module identities for existing imports and serialized objects. Fields,
canonical JSON, defaults and shallow frozen semantics stay unchanged. The
[model records contract](model-records.md) and its complete context packet
cover the data boundary; real provider and compile recovery tests remain required.

## Approved annotation compatibility — 2026-11-04

Native Python 3.14 annotations resolve without loading historical facades. Fields,
defaults and pickle paths are retained; raw annotation/signature representation
changes as explicitly approved. Cold-process tests cover ordinary get_type_hints.
