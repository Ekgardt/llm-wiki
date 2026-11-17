# Tests filed with `scripts/memory_state.py`

- `tests/test_the_adoption_pass_says_what_it_skipped.py`: The adoption pass looks past intents it cannot adopt, and the worker names them.

These files do not import `scripts/memory_state.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/capture_adoption.py`: its exact derived description.
- `scripts/flush_memory.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_adoption_pass_says_what_it_skipped.py -q`.
