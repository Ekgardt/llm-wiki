# Tests of `scripts/compile_pipeline.py`

- `tests/test_compile_part_quote_binding.py`: A timestamp candidate must contain the quoted bytes before it can bind.

`scripts/compile_pipeline.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_part_quote_binding.py -q`.
