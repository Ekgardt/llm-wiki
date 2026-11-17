# Tests filed with `scripts/integration_adapter.py`

These files do not import `scripts/integration_adapter.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/reliable_memory.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_publication_that_stopped_half_way_is_finished.py -q`.
