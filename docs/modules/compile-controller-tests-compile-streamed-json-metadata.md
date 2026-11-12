# Tests of `scripts/compile_controller.py`

- `tests/test_compile_streamed_json_metadata.py`: Canonical metadata arrays must not require a whole-array JSON allocation.

`scripts/compile_controller.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_streamed_json_metadata.py -q`.
