# The next slice: the format of a queue task's content

2026-11-02. This note cites no owner message, so it records no owner approval.
The private decision page is queue-payload-formats-decision-20261102.
Basis: `d4d9f394`, the current built-in generation
`generation-18e4487016b74277-ee8a87cf`, supplemented by reading the source.

## The problem and the boundary

`memory_queue.py` takes 137 239 o200k_base tokens even without tests. Inside
the SQLite executor sits an independent format: `PayloadValidation` and
`validate_payload_blob` with pure JSON/hash checks. Recovery in
`installed_memory_repair.py` gets the validator through `memory_queue`.
This mixes the format's owner with the executor; it is not claimed that extracting
one validator will solve the whole size of the queue or remove the current nightly errors.

The change adds `scripts/llm_wiki/formats/queue_payload.py` to the already extracted
formats module. The contiguous block from `PayloadValidation` to
`_parsed_canonical_payload` moves, including the internal helpers and `_PAYLOAD_CONTAINERS`,
together with the four existing payload limits: bytes, depth, string bytes, container
members. The limits and their semantics do not change and are not declared newly justified.
Dependencies: the standard library and the existing `formats.canonical`.

The public `PayloadValidation` and `validate_payload_blob` in the old module stay
re-exports of the same objects for compatibility. Internal working consumers
use the owner directly. There is no old implementation and no redirecting wrappers.
SQLite, leases, task states, deduplication, process start, migrations and runtime
paths stay with the queue. There is no new service, DB, setting, registration or
Protocol interface. The new own tests live in the existing folder
`tests/modules/formats/`; the full package includes new files automatically.

## Acceptance criteria

- Canonical bytes, SHA-256, the result object and refusal codes match the baseline.
- Checking the digest with `parse=False` does not turn into JSON parsing.
- Unknown/damaged bytes, repeated keys, float, NaN/Infinity, invalid
  UTF-8, non-canonical JSON and exceeding any existing bound are rejected.
- An isolated import does not load the queue, compiler, config or runtime.
  The regression first exposes the old dependency, then confirms its removal.
- The identity of the old and new public type and function is checked separately.
- The whole formats module after the change, with all tests, schemas, instructions
  and required documents, stays no larger than 100 000 tokens. The current value
  before this proposal is 80 129; the exact future budget is checked after the implementation.
- The existing scenarios of the queue, damaged payloads, recovery and transitions
  keep working. The CCN of each function is no more than 5; the AST logic of the move is cross-checked.
- Installing the unfinished branch into the live vault is not part of this slice.

Before the change, all 7 existing selected checks of `validate_payload_blob`
passed; the other 106 checks of the file are not part of this first selective baseline.
Then `test_memory_queue.py`, `test_repair_installed_memory.py`
and `test_queue_v3_corruption.py` were run in full: **150 passed in 33.04 s**. This is a check of the original
version, not of the future move. A repeated related qualification is mandatory
after the implementation.

## Alternatives and sources

Leaving the code in place with a new re-export in the reverse direction would not remove
the parser's dependency on the queue. Moving the whole queue into the folder would keep the size and
mixed responsibilities. A new service/registry/Protocol would increase complexity,
although the current contract is expressed by an ordinary function and a dataclass. The chosen option is the minimal
pure contract next to the other formats; the queue will shrink through further
separately justified slices, not by cutting it by line count.

Primary sources re-checked 2026-11-02:
[Bounded Context](https://martinfowler.com/bliki/BoundedContext.html): explicit
domain boundaries; [Import Linter contracts](https://import-linter.readthedocs.io/en/stable/contract_types/):
automatic protection of dependency directions;
[PEP 544](https://peps.python.org/pep-0544/): optional structural interfaces,
not a requirement to introduce them for every function. No new library is installed;
the existing AST checks are enough for this restriction.

Evidence: `scripts/memory_queue.py`, `scripts/installed_memory_repair.py`,
`tests/test_memory_queue.py`, `logs/queue-payload-boundary-before.json`,
`logs/queue-payload-baseline.txt`, `logs/queue-payload-full-baseline.txt`. The plan does not replace the explicit architectural approval
that `AGENTS.md` requires.


## Implementation and verification

The owner was added without changing the AST of any of the 20 moved definitions. Four
constants were moved with their old values; the queue imports the byte limit
it uses. The old internal helpers were deleted; the public objects
are re-exported without wrappers. Recovery imports the validator directly.

The new boundary guard first detected an access through memory_queue in repair.
After the move it passes. An isolated process loads and uses
the validator with imports of the queue, compiler, configuration and runtime forbidden,
as well as network calls, DBs and file writes. The existing AST check allows only
the standard library and neighbouring formats.

43 own scenarios check canonicalization, the digest, the two modes, wrong types,
immutability of the result, damaged JSON/UTF-8, numbers, duplicates and the exact
bounds of the four existing limits. They were also run against the unchanged
original block of definitions from d4d9f394: 43 passed. The existing 150 checks of
the queue, recovery and damage passed again after the move.
A check of the identity of the two public re-exports was added to the integration set.

Evidence: `logs/queue-payload-boundary-red.txt`, `logs/queue-payload-ast.json`,
`logs/queue-payload-owned.txt`, `logs/queue-payload-integrations.txt`.


Extended set after the move: **601 passed, 1 skipped in 202.04 s**
(Python 3.12). It covers the queue family, its CLI/processes, compilation through
the queue, recovery, damage, structure and the shared complexity law.
Python 3.10: **245 passed**: the formats module and the main queue integrations.
These are the related checks of this slice, not a new run of the whole project regression.
Ruff passes; 1 296 functions of the affected files were measured, maximum CCN 5.
All four limit values were separately cross-checked against the source.

The memory_queue source shrank from 137 239 to 135 934 o200k_base tokens.
It still exceeds the ceiling without tests: the slice does not complete the split of the queue.
The full formats package was recounted by an automatic test after the documentation;
added source/test files are counted automatically, and the plan is included explicitly.

Evidence: `logs/queue-payload-qualified.xml`, `logs/queue-payload-python310.txt`,
`logs/queue-payload-complexity.json`, `logs/queue-payload-qualified-selection.txt`.
