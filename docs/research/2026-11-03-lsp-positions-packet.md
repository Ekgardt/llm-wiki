# Existing LSP source positions as an isolated packet

Date: 2026-11-03. Approved decomposition mandate; qualified at
4abf9ccd. Keep scripts/lsp_positions.py and tests/test_lsp_positions.py unchanged.
The existing owner converts validated source bytes, negotiated LSP coordinates
and file URIs; code_intelligence supplies the shared value contracts.

## Current primary-source research

Checked three independent publishers today:
- Unicode Consortium: UTF-8 bytes, UTF-16 code units and UTF-32 code points differ;
  reversible encoding is not permission to split a code point or surrogate pair.
  https://www.unicode.org/faq/utf_bom.html
- Python urllib.parse: parsing splits components; validation and platform path
  policy remain the application's responsibility. Preserve Python 3.10 APIs;
  this documentation now marks that interpreter upstream-unsupported, and this
  packet does not claim otherwise or change the project compatibility floor.
  https://docs.python.org/3.10/library/urllib.parse.html
- IETF file URI specification and its current update: URI syntax and filesystem
  interpretation are distinct. RFC 9844 removes RFC 8089's reference to RFC 6874;
  it does not justify changing this local path codec during a structural task.
  https://www.rfc-editor.org/rfc/rfc8089
  https://www.rfc-editor.org/rfc/rfc9844.txt

Alternatives: a new package path creates compatibility work without reducing
coupling; generic URL parsing alone loses current rejection policy; separate
per-encoding modules duplicate shared source-boundary ownership. Qualify the
existing cohesive owner in place, with its complete unchanged tests and fixture.

## Contract and verification

Keep coordinate validation, immutable value objects, line offsets, astral Unicode,
line endings, bounded thread-safe cache and URI platform policy unchanged.
URI conversion is not a repository-containment authorization; existing tests
explicitly preserve that distinction. The fixture unicode_api.py is read as data
by the focused suite and is included in full; other code-kernel consumers keep
using the same shared fixture, without a second copy.

Declare source, test, fixture and module guide ownership. Include the full
code_intelligence source/guide, instructions, pytest bootstrap, measurement codec,
manifest and research in the <=100000-token packet. Verify isolated imports with
only code_intelligence permitted, source/test/fixture digests, existing assertions,
actual CCN for new guards, Python 3.10 and 3.12. No new cap, dependency, runtime
path, public command or live installation. Fresh graph evidence and results follow.


Qualification completed in the isolated candidate. Fresh built-in generation
`generation-18e46d21040e7dea-26a5e9a5` corresponds to 4abf9ccd. Callers were
inspected alongside source because the graph is incomplete; evidence is in
`logs/lsp-leaves-graph.json`. Production sources, existing position tests and
Unicode fixture are byte-identical to 4abf9ccd (`lsp-leaves-unchanged-proof.json`).

Both new packets together: 95 tests passed on Python 3.12 and Python 3.10.
Source/guide/bootstrap closure, ownership, structure, privacy and weights:
224 passed, 1 skipped. Ruff and actual complexity/shape checks passed for every
new test. No bug fix or production behavior change is claimed; these tests
qualify existing contracts rather than manufacturing a pre-change failure.

All 51 actual context artifacts round-tripped every path and byte under 100000
o200k_base tokens. Before this evidence paragraph, interruption was 72176 and
positions 79455; the largest packet was 96858. The final artifact report is
recorded with the committed checkpoint. Ownership is 402/3131 tracked files,
2729 still unassigned: this is not whole-project completion. The full regression
running at 4abf9ccd predates these test/documentation-only packets.
