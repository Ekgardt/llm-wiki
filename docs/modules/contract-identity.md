# Contract artifact identity

Status: foundation for the approved dependency-contract pilot. This component
does not qualify a dependency and never changes full-source closure.

`tests/module_contract_identity.py` hashes complete bytes at canonical, contained
repository paths. Schema, exact membership, byte counts and SHA-256 digests belong
to the evidence identity. Missing files, escaping paths, different schema and
changed evidence are errors. The helper neither opens runtime databases nor
executes providers.

The qualification orchestrator must independently derive required paths from
complete provider/consumer packets, interface, semantics, test inputs and checker
sources. Supplying a list is not proof that the list is complete. Runtime/compiler
and installed dependency identity, actual passing tests, access/binding validation,
semantic review and paired maintenance outcomes remain separate obligations.
No reusable passing receipt or claimed savings is implemented here.

Verification: `pytest tests/test_module_contract_identity.py
tests/modules/contract_identity -q`. Negative cases cover source, checker,
interface, semantics, test, lockfile and runtime-evidence changes; added, deleted
and malformed evidence; and path containment. Synthetic fixtures test the identity
primitive, not real provider qualification.

Full packet: `docs/modules/contract-identity-context.json`. The user ceiling remains
100000 o200k_base tokens, including this guide and full tooling source.

Source: approved `docs/research/2026-11-03-qualified-dependency-contract-proposal.md`.

## Explicit public surface identity

`tests/module_contract_surface.py` compares every locally declared public function
header and simple constant assignment with the complete interface artifact.
Defaults, parameter kinds, annotations, async shape and constant expressions
are retained. It refuses duplicate names, decorated functions and unsupported
top-level declaration shapes. Unsupported means full implementation context stays
required; this is an experimental checker capability, not a product API limit.
Imported implementation helpers are outside this explicitly declared API.

The checker does not execute stubs, assert semantic equivalence, validate consumer
access or prove runtime binding. Body-only edits deliberately leave the surface
identity equal but invalidate the complete-byte identity. The real answer-budget
surface fixture carries the full provider source and both draft contract artifacts.
