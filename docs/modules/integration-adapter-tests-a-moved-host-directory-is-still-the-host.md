# Tests of `scripts/integration_adapter.py`

- `tests/test_a_moved_host_directory_is_still_the_host.py`: A host whose directory was moved with its own variable is still captured.

`scripts/integration_adapter.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_moved_host_directory_is_still_the_host.py -q`.
