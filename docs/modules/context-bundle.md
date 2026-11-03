# Exact duplicate context bodies

llm_wiki.context_bundle packs a mapping of canonical relative names to UTF-8 bytes
into readable maintenance text. FILE records carry a JSON [path, byte length]
header, exact content and a framing newline. SAME records carry [path, prior path]
and mean precisely the same bytes. A fixed versioned preamble explains that rule.
Only byte-identical bodies can share content; source files remain untouched.

unpack_files returns all paths and exact byte strings in memory. It performs no
filesystem IO and refuses malformed, truncated or ambiguous framing, duplicate
paths, unknown versions and forward/missing references. A new serializer version
would be required to change this format. Python 3.10 stdlib is sufficient.

The test-only module-context helper snapshots every declared file, packs it,
decodes it and requires exact mapping equality before token counting. Manifests
explicitly choose exact-duplicates/v1; raw/v1 remains available. Required pytest
bootstrap and every dependency remain declared. Logical file counts are unchanged;
serialized and expanded token totals are distinct and must be reported honestly.
The smaller context is a lossless representation, not proof of cheaper task cycles.

Run `uv run pytest tests/modules/context_bundle -q -s`.
See ../research/2026-11-03-lossless-context-duplicates.md.
