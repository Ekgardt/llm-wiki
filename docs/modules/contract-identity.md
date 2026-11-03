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

## Direct access and real binding pilot probes

`tests/module_contract_access.py` checks top-level explicit named imports, direct
function calls and immutable constant reads. Private/undeclared members, namespace
imports, alternative module spellings, escaping function values, rebinding,
shadowing and recognized reflection/loading forms refuse. This conservative
grammar is a checker capability, not an application restriction: unsupported
consumer shapes keep full source. It is not a Python sandbox or complete
whole-program proof. Qualification must derive the traversed source set and
member kinds, validate test patches separately, and retain every other dependency.

The integration fixture keeps both actual source files. It checks checkout paths,
function identity and actual UTF-8 estimates, TypeError and circular-reference
behavior through the real consumer and provider. No mock can qualify these probes.
A fresh subprocess, complete provider/consumer test selection, before/after byte
identities, bounded alternative packet measurement and paired maintenance evidence
are still needed before any provider implementation can be omitted.

`tests/module_contract_calls.py` binds each directly imported function call to
the checked interface's positional-only, positional/keyword, required/default,
keyword-only and variadic parameter shapes. Defaults are not executed. Wrong
arity, unknown/duplicate keywords and calls through constant exports refuse.
Dynamic `*args`/`**kwargs` expansion retains full source because this checker
cannot establish its actual call shape. This is not runtime type or value proof.

## Public fault-injection tests

`tests/module_contract_patches.py` recognizes explicit pytest monkeypatch calls
on the consumer's real imported public function aliases. Targets derive from the
checked consumer source and interface. No manifest exemption list is accepted.
Private/undeclared members, constants, indirect reexports, changing target/fixture
bindings, dynamic member names and unsupported fixture shapes refuse. The full
replacement expression stays in the syntax walk; it cannot hide reflective access.
This permits the two original public failure-injection tests unchanged. It does
not make their mocked outcomes provider qualification.

The real fixture probe verifies pytest's MonkeyPatch type and restoration to the
actual provider function. Fresh qualification must additionally verify bindings
after the complete selected test run and run independent unmocked integrations.
Sources checked 2026-11-03: Python3.14 unittest.mock “Where to patch”
(https://docs.python.org/3.14/library/unittest.mock.html#where-to-patch) and pytest
monkeypatch documentation (https://docs.pytest.org/en/stable/how-to/monkeypatch.html).
Both describe changing lookup bindings and scoped restoration; neither supplies
a proof of arbitrary Python behavior.

Module identity values such as `__file__` and `__name__` are data reads, not
dispatch. The initial blanket double-underscore-name rejection wrongly refused
the consumer's ordinary context/boundary tests. The name guard instead rejects
actual reflective namespaces (builtins, loader/spec and class cells) and dynamic
call primitives; attribute introspection remains refused. All three original
consumer test files now participate in the static pilot probe.
