# Tests of `scripts/pyright_profile.py`

- `tests/test_pyright_profile.py`: Pinned, read-only Pyright runtime discovery contracts.

`scripts/pyright_profile.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_pyright_profile.py -q`.
