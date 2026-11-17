# Tests of `benchmark/retrieval_v2_orchestration.py`

- `tests/test_authoritative_selection_binds_each_worker_and_refuses_gaps.py`

`benchmark/retrieval_v2_orchestration.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_authoritative_selection_binds_each_worker_and_refuses_gaps.py -q`.
