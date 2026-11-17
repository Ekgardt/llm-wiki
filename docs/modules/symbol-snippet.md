# Source snippet for a symbol, resolved through the active generation

`scripts/symbol_snippet.py` and the tests that exercise them most.

CODE-02, roadmap 2026-08-28: parity with codebase-memory-mcp's `get_code_snippet`. Issue #24, B3 (2026-09-10): the generation stores one `definition` occurrence per symbol with the exact line and byte span of the definition node, and the bytes it was cut from. So a qualified name (`owner.name`, or a bare name) answers with the exact block out of the stored source — `precision: "exact"` — and says whether the file on disk still has those bytes. A node without a definition occurrence, or a repository without a generation, falls back to the 2026-08-28 recovery by definition line and indentation over the working tree, marked `precision: "heuristic"`. Bounded, deterministic, no model. Research: `docs/research/2026-08-28-symbol-snippet-mode.md`, `docs/research/2026-09-10-a-query-surface-that-answers-the-whole-graph.md`.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_common_symbol_names_share_the_graph_reader_budget.py tests/test_symbol_snippet.py tests/test_symbol_snippets_keep_the_whole_definition.py -q`.
