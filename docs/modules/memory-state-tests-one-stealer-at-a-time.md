# Tests of `scripts/memory_state.py`

- `tests/test_one_stealer_at_a_time.py`: A stale lock is checked and removed under one OS lock, so two stealers cannot make two holders.

`scripts/memory_state.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_one_stealer_at_a_time.py -q`.
