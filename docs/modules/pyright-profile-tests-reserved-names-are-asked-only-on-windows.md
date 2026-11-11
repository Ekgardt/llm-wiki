# Tests of `scripts/pyright_profile.py`

- `tests/test_reserved_names_are_asked_only_on_windows.py`: Audit 3, A1: pathlib's is_reserved() is removed in Python 3.15.

`scripts/pyright_profile.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_reserved_names_are_asked_only_on_windows.py -q`.
