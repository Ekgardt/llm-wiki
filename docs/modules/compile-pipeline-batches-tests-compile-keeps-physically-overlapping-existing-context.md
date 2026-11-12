# Tests of `scripts/llm_wiki/compile_pipeline_batches.py`

- `tests/test_compile_keeps_physically_overlapping_existing_context.py`: Known citation overlap is necessary context, independent of word ranking.

`scripts/llm_wiki/compile_pipeline_batches.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_keeps_physically_overlapping_existing_context.py -q`.
