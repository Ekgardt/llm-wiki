# Tests of `scripts/pyright_profile.py`

- `tests/test_pyright_profile.py`: Pinned, read-only Pyright runtime discovery contracts.

`scripts/pyright_profile.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_pyright_profile.py -q`.
