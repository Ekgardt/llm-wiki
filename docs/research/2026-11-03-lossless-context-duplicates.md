# Lossless duplicate bodies in maintenance context packets

2026-11-03; within the approved compression/completion mandate. This changes only
maintenance packet serialization, not product data, source files or runtime paths.

Measurement on 446bdafa found AGENTS.md and CLAUDE.md each contribute 7516 tokens
and are byte-identical, as required by the operating contract. Both logical files
must remain declared and recoverable. Excluding CLAUDE from manifests or deleting
one file would conceal required context. Repeating the same body is unnecessary.

Introduce a stdlib-only llm_wiki.context_bundle codec for developer maintenance
artifacts. A versioned human-readable bundle has FILE records carrying a JSON
path/UTF-8-byte-length header and the exact body, or SAME records explicitly naming
an earlier file with identical bytes. Metadata uses JSON arrays, avoiding ambiguous
path separators or duplicate object keys. Body lengths frame embedded marker text.
Only exact byte equality permits reuse; whitespace, Unicode normalization and
line endings are not normalized. References point backwards, so no cycles/forward
resolution or external files are needed. A reference is used only when shorter
than its literal framed record. No gzip, summaries, minification or truncation.

The decoder returns a path-to-bytes mapping without writing files. It rejects
unknown versions, malformed headers, noncanonical relative paths, duplicate paths,
unknown references, invalid lengths, truncated bodies and framing errors. Every
compact measurement decodes its generated artifact and compares every path and
byte to the source snapshot before counting actual serialized text with the same
o200k_base encoding. Both file names and alias records count toward the limit.
Uncompressed size is reported separately; the 100000 threshold continues to apply
to the actual complete context presented to the model, not a reconstructed duplicate
body that is not sent. No reduction in task-cycle model cost is claimed without
paired task measurements.

Existing raw serialization remains available. Manifests opt in explicitly via
serialization: exact-duplicates/v1. The shared measurement helper requires the new
codec source in every manifest that imports it. No required instruction, pytest
bootstrap, test or dependency is dropped. A dedicated packet owns the codec and
its tests. Emit actual bundle artifacts and verify complete round-trip equality
for every existing packet before accepting the compressed measurements.

Research checked 2026-11-03, three independent primary publishers:
- https://www.rfc-editor.org/rfc/rfc8259 — JSON string escaping and interoperable
  Unicode metadata; no replacement of body bytes with JSON-normalized content.
- https://docs.python.org/3/library/json.html — stdlib encoding/decoding; use only
  APIs available in the project's Python 3.10 baseline, no new release dependency.
- https://github.com/openai/tiktoken — count actual emitted UTF-8 text with the
  existing encoding. Character/byte savings alone do not prove token savings.
Alternatives: gzip/base64 is opaque model input; deleting duplicate logical files
breaks the manifest contract; semantic deduplication risks information loss.
Exact equality with explicit in-bundle references is inspectable and reversible.

Before code, refresh the built-in index and query the measurement call graph;
supplement with all manifests and tests/test_module_context_bootstrap.py because
test helper dynamic loading is not fully captured by the graph.

## Qualification

The fresh 446bdafa index and reverse graph queries found 28 measurement callers,
2 raw-serialization callers and 5 bootstrap callers; dynamic paths were inspected
in source too. New codec initially failed collection because it did not exist.
The codec-only tests exposed an overbroad test search that mistook the explanatory
preamble for a SAME record; the assertion now searches the actual record prefix.
No byte-equality or decoder refusal assertion was removed.

Codec Python 3.10 tests: 33 passed. Expanded module, shared helper, complexity,
structure, CI, limit and privacy checks: 804 passed, 2 skipped. Measured test files:
76 passed before the final two helper refusal cases; those cases additionally pass
in the expanded run. Every one of 28 emitted artifacts was read back from disk,
decoded and compared against every source path and byte. At the measured checkpoint
all contexts together were 2168242 serialized versus 2377935 raw tokens; the largest
was 93365 serialized versus 100854 raw. These counts describe actual representations,
not changes in source size or task-cycle model cost. Subsequent report text is also
counted; exact current values and artifact SHA-256 are regenerated in
logs/context-bundle-comparison.json. Existing files, limits and required bootstrap
remain present. Full regression continues on separate earlier snapshot 446bdafa.
