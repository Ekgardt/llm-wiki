# Tests of `scripts/evidence_graph_builder.py`

- `tests/test_evidence_graph_incremental.py`: Task 23: exact incremental Evidence Graph generation reuse.

`scripts/evidence_graph_builder.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_evidence_graph_incremental.py -q`.
