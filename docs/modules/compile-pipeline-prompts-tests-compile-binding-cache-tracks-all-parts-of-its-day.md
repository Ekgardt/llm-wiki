# Tests of `scripts/llm_wiki/compile_pipeline_prompts.py`

- `tests/test_compile_binding_cache_tracks_all_parts_of_its_day.py`: Sizing a new day must not repeat or weaken another day's authority binding.

`scripts/llm_wiki/compile_pipeline_prompts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_binding_cache_tracks_all_parts_of_its_day.py -q`.
