# Tests of `scripts/pyright_session.py`

- `tests/pyright_session_helpers.py`: Helpers shared by the test_pyright_session*.py files and the test files importing them.

`scripts/pyright_session.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/pyright_session_helpers.py -q`.
