# Graph storage-value contract packet

Date: 2026-11-03. Prepared at 1214d765 under the approved decomposition mandate.

The already independent scripts/graph_storable.py remains byte-identical. Its
owner covers text admissibility, identity-key fallback and recursive metadata
adaptation; it does not own graph schemas, SQLite writes or source extraction.
The existing reader/writer integration tests stay intact. New direct tests pin
empty/control/length edges, deterministic identity digest (including surrogate
encoding), finite/nonfinite float conversion, dates, nested tuples/mappings,
Unicode replacement and untouched unsupported scalars. They document current
behavior rather than claiming a general JSON sanitizer. Source keys and node
identities remain controlled by callers; transformations are not a new schema.

A packet combines exact source, direct tests, source research and bootstrap.
Import guards permit only stdlib and both current import forms (top-level and
scripts.graph_storable). No forwarding module, new package, installation,
dependency or threshold is introduced. No duplicate algorithms are created.
Before tests: refresh built-in graph, inspect source funnels and writer validation.
After: byte proof, original consumer tests, actual CCN/shape, Python 3.10/3.12,
complete token measurement and lossless packet round trip.

## Primary-source research

Checked 2026-11-03, three independent publishers:
- IETF RFC 8259 sections 6/8.2 distinguish valid numbers from NaN/Infinity and
  describe interoperability problems for unpaired surrogates. Preserve the
  project's existing conversion policy; the RFC does not prescribe float-to-text.
  https://www.rfc-editor.org/rfc/rfc8259.txt
- Python codecs documents distinct encode error handlers. UTF-8 `replace` on
  encoding yields `?` for an unencodable surrogate; `surrogatepass` preserves its
  code point for the identity digest. Do not substitute one for the other.
  https://docs.python.org/3/library/codecs.html#error-handlers
- SQLite documents engine length limits and their explicit testing. Those are
  not the source of this project's 4096-character identity threshold: its own
  comment records that value as predating measurement. Preserve it without
  inventing a safety justification or changing it under decomposition authority.
  https://sqlite.org/limits.html

Alternatives: truncate identities (changes collision behavior), sanitize at each
extractor (duplicates policy), relax the writer (changes validation), or call a
new serializer here (adds dependency and accepted-input changes). Retain the one
shared existing stdlib owner. Its unknown historical threshold rationale remains
an explicit limitation, and unsupported objects are still rejected by consumers.
Historical evidence: docs/research/2026-09-14-the-rest-of-the-readers-before-the-writer.md.

## Implemented evidence

Built-in generation-18e46f1d757895e9-4bba8a99 and caller graph/source inspection
confirmed the shared funnels. Production is byte-identical; the original nine
reader/writer tests are unchanged and passed before qualification. Direct value
cases include the independently hashed surrogatepass byte sequence ED A0 80 0A.
Three-owner/consumer checks initially 298/3; final Python 3.10 308/3. Actual
CCN/shape and Ruff passed. Initial complete packet 72013 tokens, and all 59
packets round-trip; largest 97405 before evidence additions. No sanitizer,
identity change, new threshold, bug fix or live installation is claimed.
Final focused owners 60 passed; global guards 1445 passed/two skipped in
146.18 seconds. The final artifact rebuild includes this evidence.
