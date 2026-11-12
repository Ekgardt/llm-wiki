# Tests of `scripts/mcp_server.py`

- `tests/test_every_mode_checks_the_directory_it_is_given.py`: Audit 3, B1: the task-shaped modes validate `directory` like the older ones.

`scripts/mcp_server.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_every_mode_checks_the_directory_it_is_given.py -q`.
