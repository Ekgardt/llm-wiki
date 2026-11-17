# Tests of `benchmark/retrieval_v2_outputs.py`

- `tests/test_benchmark_outputs_are_created_once_and_refuse_moved_paths.py`

`benchmark/retrieval_v2_outputs.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_benchmark_outputs_are_created_once_and_refuse_moved_paths.py -q`.
