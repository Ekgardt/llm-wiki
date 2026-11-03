# Exact record contexts

Record catalogs are test-only maintenance metadata for large documentation,
benchmark JSON, uv.lock and the root CHANGELOG.md. They never rewrite the source, replace executable
imports, or waive full instructions/owner contracts. Each catalog binds the
original tracked path, byte length and SHA-256 and a complete ordered partition
of named logical records. Offsets are UTF-8 bytes, not character or UTF-16 indexes.

The validator checks every record, including unselected ones: strict JSON fields,
unique IDs, whole-source identity, contiguous nonempty intervals through EOF,
individual hashes, UTF-8 boundaries and byte-for-byte concatenation. A catalog is
not proof of semantic independence: choosing record boundaries and necessary
cross-record context requires format-aware review. Hashes detect changed bytes;
they do not authenticate a hostile editor or prove a source statement true.

A packet may select records through `records: [{catalog, ids}]`. It includes the
complete catalog, validator/loader source and this full guide. The lossless bundle
uses reserved `@record/<source>#<id>` entry names for original record bytes;
provenance is supplied by the included catalog. Unknown or duplicate selections,
source aliases/escape, stale metadata and serialization-key conflicts fail.
Existing full-source/test/bootstrap/contract closure remains mandatory.

`ownership.records` is separate from shared context and may claim only selected
records. There is one catalog authority per source. Duplicate record ownership,
competing catalogs and whole-file/record ownership conflicts fail. A source stays
unassigned until every record has one owner; the v2 inventory reports partial
coverage and missing IDs explicitly and counts a fully covered source only once.
The catalog file itself also needs a normal whole-file owner.

Do not split by a token quota or use record views to hide required source. Actual
historical/data catalogs require whole-source reconstruction plus semantic review
and complete packet measurement <=100000 o200k_base tokens. A smaller excerpt is
not project completion or demonstrated savings on a full LLM task.

Run the record validation, context loading and record ownership tests, existing
context/ownership regressions and every packet budget. The first pure validator
passed absence/red and malformed-path/red qualification. Measurement, ownership
and whole-authority omission regressions now pass. Registering actual catalogs is
a distinct next step tracked in the research.
Source: ../research/2026-11-03-large-artifact-context-investigation.md.

Shared canonical contract: [Lossless context serialization](context-bundle.md).
