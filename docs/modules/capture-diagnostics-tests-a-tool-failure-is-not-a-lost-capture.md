# Tests of `scripts/capture_diagnostics.py`

- `tests/test_a_tool_failure_is_not_a_lost_capture.py`: A failed tool call or telemetry write is reported on its own, not as a lost capture.

`scripts/capture_diagnostics.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_tool_failure_is_not_a_lost_capture.py -q`.
