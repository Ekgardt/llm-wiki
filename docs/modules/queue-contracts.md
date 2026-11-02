# Queue data contracts

The module owns the 20 existing public queue records and errors listed in
`queue_contracts.__all__`. It depends only on the standard library. It does not
open a database, start a worker, acquire ownership or validate new business rules.
Frozen records retain the original fields, defaults and methods.

Consumers import types from `llm_wiki.queue_contracts`; queue operations remain
in `memory_queue`. That facade reexports the identical objects. Their historical
`memory_queue` module names are deliberately retained for pickle compatibility,
including old running parents and newly started children. Pickle can load the
facade; isolated serialization is not promised. Do not load untrusted pickle data.
These aliases remain necessary until the existing serialization contract is
explicitly migrated and all active consumers support that migration.

## Own maintenance context

`queue-contracts-context.json` enumerates the source, instructions, architecture,
design and own tests/data. The entire test directory counts recursively. The
budget is the user's 100,000 o200k_base tokens, not a source-only measurement.
No runtime dependency is hidden outside this packet; the documented pickle
facade is tested at the integration layer.

Run the isolated contract suite without the global runtime conftest:

```
uv run pytest --confcutdir=tests/modules/queue_contracts tests/modules/queue_contracts -q -s
```

The guard rejects consumers importing these types through the executor, including
aliased and local imports. A fresh isolated process blocks non-stdlib imports,
filesystem mutations, database, network and subprocess activity during import
and construction. The fixture records trusted protocol-4 bytes from fec3fbaf.

## Integration scope

`tests/test_memory_queue.py` checks all 20 aliases, exact old/new serialized bytes,
old values and actual spawn transfer in both directions. These tests intentionally
load the executor and are not part of the isolated module packet. Queue, capture,
compile completion, recovery and CLI tests verify behavior across the boundary.
See the implementation evidence in
`../research/2026-11-02-queue-contracts-boundary.md`.

This extraction does not finish modularizing memory_queue, whose source still
exceeds 100,000 tokens. The candidate branch is not installed into the live vault.
