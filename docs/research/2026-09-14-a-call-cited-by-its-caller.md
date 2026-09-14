# A call cited by its caller, not its line

Dated 2026-09-14. The research before a change to `benchmark/code-parity-v2.json`.

## What was found

The full test suite on the day's fixes failed
`tests/test_parity_gold_resolves.py::test_every_gold_citation_resolves_in_the_tree[T03]`:
`scripts/retrieval.py:1591 does not name _fusion_weights`. Task T03 ("Which project
functions does fuse_rrf call?") cites each call by its line number inside `fuse_rrf`.
Registering retrieval's inference thread (`docs/research/2026-09-14-no-model-running-at-exit.md`)
added lines above `fuse_rrf`, so every line moved while the gold stayed true.

This is the same drift the suite already solved for definitions on 2026-09-13: a
citation may name `path (def name)` and leave the line to be resolved
(`docs/research/2026-09-13-what-the-stands-must-show-after-the-verdict-cache.md`).
T03's calls live inside one definition, so the definition is their stable anchor.

## Practice on this date

- An anchor that moves with unrelated edits turns a true benchmark red; stable
  identifiers — a symbol rather than a byte or line position — are how code
  navigation tools keep references valid across edits (the SCIP index format names
  occurrences by symbol, with ranges as data of one snapshot)
  ([SCIP code intelligence protocol](https://github.com/sourcegraph/scip)).
- The graded part of T03 is its `must` terms — the called names — and they do not
  change here.

## The decision

T03's citations name `scripts/retrieval.py (def fuse_rrf)` and list the calls its body
makes; the dated line range stays as a historical note. The graded `must` terms are
unchanged, so no score moves.

Files: `benchmark/code-parity-v2.json`,
`docs/research/2026-09-14-a-call-cited-by-its-caller.md`.
