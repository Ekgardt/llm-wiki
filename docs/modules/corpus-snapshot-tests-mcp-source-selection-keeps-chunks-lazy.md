# Tests of `scripts/corpus_snapshot.py`

- `tests/test_mcp_source_selection_keeps_chunks_lazy.py`: Context source selection must not expand a corpus before compilation.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_mcp_source_selection_keeps_chunks_lazy.py -q`.
