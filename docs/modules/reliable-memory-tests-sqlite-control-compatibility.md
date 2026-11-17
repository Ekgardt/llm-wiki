# Tests of `scripts/reliable_memory.py`

- `tests/test_sqlite_control_compatibility.py`: Historical import/pickle identity survives new SQLite control ownership.

`scripts/reliable_memory.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_sqlite_control_compatibility.py -q`.
