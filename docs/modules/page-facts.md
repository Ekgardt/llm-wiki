# Cited page facts

This owner qualifies existing `scripts/page_facts.py` in place without changing
production bytes. It reads one caller-supplied page and returns its declared type,
authority, confidence and status together with mtime-based age, the existing type
age limit and provenance weight. It does not establish citation entailment or
truth. The MCP consumer retains citation selection, coverage, confidence
aggregation, warning rendering and the public response envelope.

The complete implementations and guides of okf_types, page_status, provenance
and corpus_membership_policy remain dependencies in the packet. There is no MCP,
collector, database or model dependency in this reader. No runtime interface,
cache, new dependency or relocation is introduced.

## Existing IO and interpretation contract

Empty paths and unreadable pages return None. Paths are trusted caller input:
this helper is not a containment or symlink-security boundary. It opens UTF-8
text with invalid bytes ignored and reads at most 8192 decoded characters despite
the historical MAX_FRONTMATTER_BYTES name and byte-oriented comment. Qualification
preserves that behavior and adds a non-ASCII regression; it does not silently
turn the existing character bound into a byte bound. The current 8192 limit is
preexisting and justified in the source by observed frontmatter sizes.

A regex reads the initial frontmatter and four fields; this is not a general YAML
parser. Fields are case-folded, quotes and surrounding whitespace handled by the
existing patterns. Missing status defaults to active. Age uses current wall time
and file mtime, clamps future mtimes to zero and remains unknown if stat fails.
Read and stat are separate observations, not a snapshot transaction. Never-archive
types do not age; other pages age strictly after their existing type limit.
Declared confidence maps to existing values; unknown values remain unstated.

## Qualification and alternatives — 2026-11-03

The approved autonomous decomposition plan permits existing cohesive owners to
stay at their public paths. Combining this reader with MCP would retain an
unbounded composition dependency; adding a protocol wrapper or copying policy
would add machinery without separating state. Keeping the source and its full
concrete dependencies is sufficient here. The dependency-contract experiment
is not needed for this owner and grants no source omission.

Research checked on this date:
[Fowler bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[Import Linter dependency contracts](https://import-linter.readthedocs.io/en/stable/contract_types/),
[PEP 544](https://peps.python.org/pep-0544/) and
[Python 3.14 text IO](https://docs.python.org/3.14/library/io.html#io.TextIOBase.read).
They support cohesive responsibility, explicit dependency checks and the stated
text-read semantics; they do not prove correctness of this implementation.
No new dependency version or additional framework is required.

The original August24 answer-quality research stays complete as dated historical
evidence, not a claim about today's measured confidence accuracy. New reader
checks cover real files, normalization/defaults, age boundaries, missing evidence
and Unicode head length. Existing MCP grounded-recall integration remains intact
under its own module owner. Static and isolated-import
checks reject upward dependencies. Every complete packet must fit 100000 tokens;
packet qualification does not establish whole-task LLM savings.

## Structural reference retained from the overview

[Cited page facts](page-facts.md) owns the existing caller-path reader,
frontmatter interpretation and mtime-based age facts. Complete taxonomy, status
and provenance policy sources remain dependencies. MCP retains citation selection,
quality aggregation and warnings. This reader provides no containment or entailment
proof; its existing decoded-character head bound and production bytes are preserved.
