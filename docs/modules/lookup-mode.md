# Print the retrieval mode search uses on this vault, and what it searches

`scripts/lookup_mode.py` and the tests that exercise them most.

HYBRID — the active evidence generation has complete vectors: BM25 + dense retrieval + reranking, the default of `search_memory.py` and `recall`; a question about relations also asks the evidence graph (GRAPH). BASE — a generation without complete vectors: BM25 alone. DIRECT — no active generation: search reads Markdown directly, bounded by its deadline, and every hit says `no_active_generation`.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
