# Contract packet construction

Status: experimental builder retained; canonical publication is in progress under
the approved Nov4 rollout. This owner composes provider/consumer contracts and
fresh execution evidence into exactly reconstructable bounded packets. Real test
execution belongs to contract-qualification; static layout belongs to contract-identity.
The experimental callable and test paths are unchanged. Both historical paired-task
reports remain whole in this packet, including failures and unallocated costs.
A smaller packet does not prove full-cycle savings or whole-project completion.
No live installation, provider stub or hidden source dependency is introduced.

Full packet: docs/modules/contract-packets-context.json. Tests include the original
candidate guards and this owner's complete budget check. The100000-token ceiling
and source/ownership/byte reconstruction checks are unchanged.

The full-source workflow remains the paired control. Count reads, replies, failed
attempts, corrections, verification and coordination; report unmetered usage.
Both complete reports are included: [first pair](../research/2026-11-03-dependency-contract-paired-maintenance-result.md)
proves limited own/cross-test correctness but lacks full usage; [instrumented pair](../research/2026-11-03-dependency-contract-instrumented-pair.md)
retains tokenizer failure/retry and participant totals. Coordinator costs remain
unallocated; full-cycle/project savings are unproven.

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

Use the usual `scripts` import root. Outputs are fresh test logs, JUnit, phase
observations, full input identity and exact candidate.bundle. The comparison keeps
full_cycle_savings null. Additional omitted transitive implementations refuse until
their dependency chain qualifies. Builder/helper bytes are independently bound
before/after construction in builder.json, whose digest enters the binding record.



Source: approved docs/research/2026-11-04-qualified-contract-rollout-proposal.md;
private contract-packet-owner-decision-20261104 records the responsibility split.
