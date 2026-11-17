# Tests of `scripts/compile_controller.py`

- `tests/compile_streamed_archive_batches_helpers.py`: Helpers shared by test_compile_streamed_archive_batches.py and the test files importing them.

`scripts/compile_controller.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/compile_streamed_archive_batches_helpers.py -q`.
