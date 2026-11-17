# Tests filed with `scripts/capture_diagnostics.py`

- `tests/test_queue_processor_failure_names_itself.py`: A failed queue processor leaves its reason on disk, not just a code.

These files do not import `scripts/capture_diagnostics.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/queue_core.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_queue_processor_failure_names_itself.py -q`.
