# Bounded multi-hop structural queries over the active Evidence Graph generation

`scripts/graph_query.py` and the tests that exercise them most.

CODE-01, roadmap 2026-08-18 section 12: parity with codebase-memory-mcp's `query_graph`, without exposing a query language. The query is a closed JSON pipeline — one start filter, caller-specified edge hops, one limit — so there is no injection surface and every step runs against the engine's own row, work, and deadline ceilings. The engine refuses silent truncation by raising (`query row ceiling exceeded`, measured 2026-08-28); this module turns every such refusal into a named part of the answer. Research: `docs/research/2026-08-28-bounded-graph-query-mode.md`.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_graph_query.py -q`.
