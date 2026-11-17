# Tests of `scripts/llm_wiki/queue_task_lineage.py`

- `tests/test_capture_and_compile_lineage_refuses_what_its_history_contradicts.py`

`scripts/llm_wiki/queue_task_lineage.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_capture_and_compile_lineage_refuses_what_its_history_contradicts.py -q`.
