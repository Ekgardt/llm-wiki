# Tests of `scripts/integration_adapter.py`

- `tests/test_the_opencode_plugin_reads_the_events_opencode_sends.py`: The OpenCode plugin and the adapter read the event shapes OpenCode sends.

`scripts/integration_adapter.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_opencode_plugin_reads_the_events_opencode_sends.py -q`.
