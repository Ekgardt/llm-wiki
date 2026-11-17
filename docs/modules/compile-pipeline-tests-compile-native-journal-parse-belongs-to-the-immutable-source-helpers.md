# Tests of `scripts/compile_pipeline.py`

- `tests/compile_native_journal_parse_belongs_to_the_immutable_source_helpers.py`: Helpers shared by test_compile_native_journal_parse_belongs_to_the_immutable_source.py and the test files importing them.

`scripts/compile_pipeline.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/compile_native_journal_parse_belongs_to_the_immutable_source_helpers.py -q`.
