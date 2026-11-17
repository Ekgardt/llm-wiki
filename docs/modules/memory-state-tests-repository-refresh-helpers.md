# Tests filed with `scripts/memory_state.py`

- `tests/repository_refresh_helpers.py`: Helpers shared by test_repository_refresh.py and the test files importing them.

These files do not import `scripts/memory_state.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/evidence_reader_cache.py`: its exact derived description.
- `scripts/installed_memory_repair.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/repository_refresh_helpers.py -q`.
