# Reliability defaults

Approved 2026-11-02. `llm_wiki.reliability_defaults` owns the existing frozen
ReliableMemoryDefaults dataclass and DEFAULTS instance. It imports only stdlib.
All 21 values, field types/order, constructor and frozen semantics are unchanged.
These are existing reliability contract values, not new operator configuration.
The ratio and original policy provenance remain documented beside the definition.

reliable_memory retains identical class/object aliases and the class keeps
`__module__ = 'reliable_memory'` for legacy pickle compatibility. Import and
construction need no durability module. Pickle can load the old facade; isolated
serialization is not claimed. The aliases and module metadata remain required
until a deliberately migrated serialization/import contract permits retirement.
There is one class definition and one DEFAULTS constructor, not parallel policy.

Twelve production from-import consumers and doctor's qualified read now refer
directly to the owner. Their other durability operations retain their owners;
the doctor import remains local to its heartbeat loop. Guard tests inspect both
from-import and module aliases, including local imports and wildcard imports.
They do not detect arbitrary dynamic Python code.

Own tests live under tests/modules/reliability_defaults. Their trusted fixture
records all original values and protocol-4 samples generated before extraction.
The complete context manifest includes entire source/tests/data, shared rules,
structure and design. The pinned tokenizer enforces 100,000 o200k_base tokens.
Consumer scanning is an architecture gate over the checkout; consumer integration
is additional context and qualification, not an isolated unit task.

```sh
PYTHONPATH=scripts uv run pytest --noconftest tests/modules/reliability_defaults -q -s
```

tests/test_reliable_memory.py retains the original explicit expected-value test
and checks facade identity and exact old/new pickle bytes for the class, default
and customized instances. Fresh-interpreter tests reject reliable_memory, queue
and state imports while constructing the owned defaults. IO/SQLite, queue,
transactions, ownership, capture, archives, repair and backup scenarios provide
additional integration evidence. Python 3.10 remains the compatibility floor.

See docs/research/2026-11-02-queue-renewal-boundaries.md for approval, alternatives,
sources and staged qualification. No runtime path, storage schema, limit value or
installed configuration changes. This packet alone does not finish modularizing
the queue or establish lower full-task LLM cost.

## Reliability defaults and queue heartbeat (approved 2026-11-02)

`scripts/llm_wiki/reliability_defaults.py` owns the existing frozen
ReliableMemoryDefaults and DEFAULTS. Values and fields are unchanged;
reliable_memory reexports the same objects and the class retains its historical
pickle module name. Import/construction are independent; pickle may load the
legacy facade. Confirmed consumers import defaults from their owner directly.

`scripts/llm_wiki/queue_heartbeat.py` owns _LeaseHeartbeat, _SourceFenceHeartbeat,
their bounded join helper and existing stop floor. Narrow typing.Protocol
interfaces describe the queue methods already called. SQL, fencing and wait
implementations stay with queue backends. Thread lifecycle, defaults, renewal
algorithm and error policy are preserved; lease_renewal remains the scheduler.
Only stdlib, reliability_defaults, queue_contracts and lease_renewal are imported.
Required old class names are identical aliases, not wrappers.

Each has own tests under `tests/modules/<module_name>/` and a guide/context
manifest under `docs/modules/` using the hyphenated name. Complete packets obey
the 100,000-token ceiling. No runtime path, schema, daemon or numeric policy
changes. Approval: `knowledge/notes/queue-renewal-boundaries-decision-20261102.md`.
