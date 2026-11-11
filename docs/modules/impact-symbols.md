# What the current diff touches, transitively, as code symbols (issue #24, B5)

`scripts/impact_symbols.py` and the tests that exercise them most.

`impact_analysis.analyze_impact` maps a diff to the symbols whose definition spans it changed and walks confirmed reverse edges from them, but keeps only decisions, pages, tests and checkpoints: every function, method or class it reached is dropped. This module answers that missing half from the same generation — the code symbols that call, import or inherit a changed symbol within eight hops — and `mode=impact` carries it as `affected_symbols` beside the unchanged `affected` groups. Bounded seeds, depth, work and rows; a cut answer says so. The walk follows every resolved edge, including the extractor's medium-confidence ones, and the answer names that difference from the confirmed-only `affected` groups. Research: `docs/research/2026-09-10-a-query-surface-that-answers-the-whole-graph.md`.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
