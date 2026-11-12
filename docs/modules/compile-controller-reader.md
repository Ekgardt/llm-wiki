# Reading an admitted compile snapshot archive back

`scripts/llm_wiki/compile_controller_reader.py` and the tests that exercise them most.

It is one concern part of `scripts/compile_controller.py`; callers and tests import
and patch this part where a name is looked up, not the facade.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_verified_original_ranges.py -q`.
