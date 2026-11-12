# Tests of `scripts/llm_wiki/compile_pipeline_sources.py`

- `tests/test_choice_measure_reuses_partition_parsing.py`: Choice sizing reuses pure partition parsing, never a source-validation verdict.

`scripts/llm_wiki/compile_pipeline_sources.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_choice_measure_reuses_partition_parsing.py -q`.
