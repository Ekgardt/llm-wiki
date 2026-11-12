# Tests of `scripts/integration_adapter.py`

- `tests/test_capture_failure_trail_names_the_event.py`: The durable failure trail must name which invocation failed.

`scripts/integration_adapter.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_capture_failure_trail_names_the_event.py -q`.
