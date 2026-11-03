# Fresh dependency-contract qualification experiments

Status: experimental pilot under the owner's explicit approval. The canonical
full-source gate and 100000 o200k_base ceiling are unchanged. A candidate packet is
not accepted merely because these checks pass; paired maintenance correctness and
full-cycle token/verification cost remain required before adoption.

This owner contains execution, evidence validation and the real answer-cost /
answer-budget integration fixture. The static byte/surface/access/call/patch
checks remain with [contract identity](contract-identity.md). The integration
packet includes both complete production sources and every local tooling import.

Every experiment executes real tests anew. It does not accept a previous passing
record as authority. Provider and consumer test files derive from their ownership
manifests. Explicit integration tests must belong to this integration owner.
Both existing complete packets and this integration packet must fit the original
limit. Required artifact membership and full bytes are checked before and after
execution, including implementation, contracts, tests, manifests and helper code.

A fresh Python subprocess uses an empty bytecode-cache location. The standard
project LONG_TIMEOUT bounds test execution and remains configurable through the
existing test timeout scale. Test filtering/deselection, missing tests or runtime
probe evidence, skipped/xfail cases, nonzero exits and changed source refuse the
experiment. The probe verifies actual module origins, imported function identity,
constant values, pytest fixture identity and restoration after test teardown.
Independent unmocked assertions check actual estimator results and errors.

Records include actual runtime/test version, selected and observed tests, outcome
counts and artifact identities. They describe that execution, not a portable
promise or a cache entry. Installed-dependency or compiler changes cannot reuse a
prior pass because every new experiment runs the real tests again. Finite tests
and the supported static grammar are not a sandbox or a proof of arbitrary
Python behavior. Other dependency implementations remain full source.

The pilot manifest identifies the real provider, consumer, interface, semantics
and integration owner. Experimental bundle construction preserves all consumer
own files and byte reconstruction. Only a verified explicit provider boundary may
be represented by its full interface and semantics. Any other missing dependency
refuses. Reports and candidate bundles are reproducible derived test artifacts;
no product service, installation, persistent runtime root or default source-closure
exception is introduced.

The full-source workflow remains the control for the paired task. Measure all
context reads, replies, retries, corrections and verification, and report any
missing usage instrumentation. A smaller first prompt alone is not an efficiency
result.

Source: approved `docs/research/2026-11-03-qualified-dependency-contract-proposal.md`;
private fresh-runner boundary decision 2026-11-03; existing project test-timeout
contract and the primary sources recorded in the approved proposal.

## Reproduce the experiment

From the checkout, use its locked Python 3.14 base/dev environment. Supply a new
output directory for every invocation; existing evidence is never overwritten.

```python
from pathlib import Path
from tests.module_contract_candidate import build_candidate

root = Path.cwd()
result = build_candidate(
    root,
    root / 'docs/modules/answer-cost-dependency-pilot.json',
    root / 'logs/contract-pilot-new-run',
)
print(result)
```

The invocation needs the project's usual `scripts` Python import root, as pytest
does. It writes fresh test logs, JUnit and per-phase runtime observations, a full
input identity, and a byte-reconstructable `candidate.bundle`. The comparison
explicitly reports `full_cycle_savings: null` until a real paired task supplies
that measurement. These generated records are analysis artifacts, not new live
installation state. This initial candidate builder refuses additional missing
transitive implementations; a more complex boundary needs its own qualified
dependency chain before this restriction can be widened.
