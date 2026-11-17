# Graph-neighbor retrieval boost for hybrid search

`scripts/graph_neighbors.py` and the tests that exercise them most.

When BM25+Vector find page A, pages that A links to via [[wikilinks]] get a relevance boost. This is the 3rd retrieval signal (after BM25 and Vector) that akitaonrails/ai-memory uses for triple-fusion RRF.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_graph_neighbors.py -q`.
