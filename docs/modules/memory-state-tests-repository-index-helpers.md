# Tests filed with `scripts/memory_state.py`

- `tests/repository_index_helpers.py`: Helpers shared by test_repository_index.py and the test files importing them.

These files do not import `scripts/memory_state.py`, the module they are filed with, and they import no product code directly.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/repository_index_helpers.py -q`.
