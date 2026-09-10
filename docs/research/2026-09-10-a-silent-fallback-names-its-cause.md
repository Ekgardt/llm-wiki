# A silent fallback names its cause

Date: 2026-09-10. Trigger: audit findings M5 and M6. The model-load failure
is named once (`search_memory._note_embedder_unavailable`, 2026-09-07), but
the fix stopped one function short: `_embed_texts`, `_cached_vectors`,
`_encoded_page_vectors` and `_persisted_vector_metadata` still end in
`except Exception: return None`, so an encode that raises (OOM, tokenizer
error, a corrupt `.npy`) looks exactly like "no vectors yet". On the
generation side, `_active_manifest_for`, the dense backend, the lexical
backend and `_attach_graph` turn any exception into a label
(`generation_unavailable`, `generation_vectors_unavailable`,
`generation_corrupt`) and the exception itself reaches nothing: a nightly
that produced an unreadable `vectors.npy` degrades every search to lexical
and the operator finds it only by reading traces.

## Sources

1. `search_memory.embedder_unavailable_reason` and its tests
   (`tests/test_embedder_unavailable_names_its_reason.py`): the shape that
   already works — one redacted, bounded reason per kind, said once on
   stderr, readable by a caller.
2. `secret_redact.describe_error` (this evening): `Class: redacted message`.
3. The MCP health resource (`_vault_status`) now carries the warm-up state;
   the same answer is where a degraded retrieval path belongs.

## Decision

`search_memory.note_degradation(kind, error)` records `Class: redacted
message` under `kind`, says it once on stderr, and
`degradation_reasons()` returns the record; the eight sites above call it
with their kind (`vector_encode`, `vector_cache`, `vector_persist`,
`generation_manifest`, `generation_vectors`, `generation_lexical`,
`generation_graph`). The trace labels stay as they are; the health resource
carries `retrieval_degradations`. Behaviour of the search is unchanged.

Files: `scripts/search_memory.py`, `scripts/retrieval.py`,
`scripts/mcp_server.py`, `tests/test_search_ranking.py`,
`tests/test_mcp_server.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-memory-and-retrieval.md`.
