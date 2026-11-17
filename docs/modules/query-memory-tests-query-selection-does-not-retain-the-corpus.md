# Tests of `scripts/query_memory.py`

- `tests/test_query_selection_does_not_retain_the_corpus.py`: A shortlist must not retain unrelated corpus chunk objects.

`scripts/query_memory.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_query_selection_does_not_retain_the_corpus.py -q`.
