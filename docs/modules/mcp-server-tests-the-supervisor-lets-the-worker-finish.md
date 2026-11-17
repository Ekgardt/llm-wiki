# Tests of `scripts/mcp_server.py`

- `tests/test_the_supervisor_lets_the_worker_finish.py`: The supervisor waits out the worker's own settling, and survives a file vanishing.

`scripts/mcp_server.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_supervisor_lets_the_worker_finish.py -q`.
