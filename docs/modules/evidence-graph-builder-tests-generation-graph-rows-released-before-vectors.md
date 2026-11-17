# Tests of `scripts/evidence_graph_builder.py`

- `tests/test_generation_graph_rows_released_before_vectors.py`: Persisted graph working rows must not overlap the vector encoder's lifetime.

`scripts/evidence_graph_builder.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_generation_graph_rows_released_before_vectors.py -q`.
