# Shared strict UTF-8 prefix boundary

Date: 2026-11-03. Prepared after the maintenance test-import correction.

Move the existing _utf8_prefix function from code_navigation_renderer into
scripts/llm_wiki/utf8_text.py with an exact body/signature. The owner is stdlib-only,
performs no I/O and accepts the caller's existing byte ceiling. The renderer keeps
an alias, preserving its public rendering behavior, strict encoding and existing
truncation flags. Keep function module identity for serialization compatibility.
No new limit or fallback, and no new Unicode normalization policy.

The near-term second consumer is queue worker error-code correction, after the
transfer is independently checked. That caller will explicitly handle unusual
exception-type names before calling this strict primitive; do not weaken the
renderer to accommodate that different input policy. No new general text framework.

## Current primary research

Checked 2026-11-03, three independent publishers:
- IETF RFC 3629 defines variable-length UTF-8 and scalar boundaries. Byte slicing
  alone is insufficient for a valid text prefix.
  https://www.rfc-editor.org/rfc/rfc3629.txt
- Python codecs documents strict encoding/decoding errors. The existing helper
  encodes valid text first, then uses the decoder's failure start only to remove
  an incomplete trailing scalar. Invalid original Unicode still raises.
  https://docs.python.org/3/library/codecs.html#error-handlers
- Import Linter documents independent/layered boundaries. Reuse the one existing
  pure implementation below consumers rather than importing the whole navigation
  renderer into queue execution or copying its clipping algorithm.
  https://import-linter.readthedocs.io/en/stable/contract_types/

Alternatives: duplicate the algorithm, import renderer from queue, use an
incremental decoder solely to create a new implementation, or silently ignore
malformed original Unicode. Retain existing code, with focused original-baseline
cases and red-to-green import/ownership guards. Prove exact AST and legacy pickle,
run existing renderer consumers, actual CCN/shape and Python 3.10/3.12. Measure the
complete packet including tests, transitive context and lossless framing.
The known queue byte-bound bug is not fixed by extraction alone. No deployment.

## Transfer qualification

Fresh generation-18e4704110c7c4fa-bf4fe008 and source inspection confirmed two
renderer callers. Original function and all remaining renderer definitions are
AST-identical; renderer test bytes are unchanged and the original function pickle
is byte-identical. The combined original-owner baseline had 31 passing cases;
seven boundary guards failed before the two-owner transfer. Related checks 97,
final Python 3.10 99, queue/capture consumers 138, global guards 1495/two skipped.
Ruff and actual complexity passed. Initial packet 71489 tokens; all 61 artifacts
round-trip under 100000. Final rebuild includes this evidence. This transfer does
not claim the pending queue error-code correction as completed.
