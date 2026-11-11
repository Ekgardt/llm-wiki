# Tests of `scripts/llm_wiki/compile_pipeline_batches.py`

- `tests/repair_capacity_repartitions_complete_source_work_helpers.py`: Helpers shared by test_repair_capacity_repartitions_complete_source_work.py and the test files importing them.

`scripts/llm_wiki/compile_pipeline_batches.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/repair_capacity_repartitions_complete_source_work_helpers.py -q`.
