# Docstrings say what the code does now

Date: 2026-09-25. Audit item C-26 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `answer_cost` said the wire form is `indent=2`; since 2026-09-13
  `mcp_server._rendered_envelope` renders compact JSON (`separators=(",", ":")`). It also
  named `fallback_reason` as an input of `_recall_components`, which reads
  `signals_used`, `reranker_applied`, `reranker_fallback_reason` and the index age.
- `search_memory._lazy_generation_query_encoder` said the cold load "is about ten seconds";
  the 2026-09-24 profile measured 2.7 s, and the straggler is now a thread
  `inference_threads` waits for at exit, not a bare daemon.
- `lookup_mode` said search always runs HYBRID; since C-22 a relation question runs GRAPH.

## Source

- PEP 257, https://peps.python.org/pep-0257/ (fetched 2026-09-25): "The docstring for a
  function or method should summarize its behavior and document its arguments, return
  value(s), side effects, exceptions raised, and restrictions on when it can be called (all
  if applicable)."

## Decision

- Correct the three texts; keep the historical measurement, dated, where it explains why the
  code is shaped as it is. No behaviour changes.

## Files

- `scripts/answer_cost.py`
- `scripts/search_memory.py`
- `scripts/lookup_mode.py`
- `CHANGELOG.md`
