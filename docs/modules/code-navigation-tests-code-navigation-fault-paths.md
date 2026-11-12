# Tests of `scripts/code_navigation.py`

- `tests/test_code_navigation_fault_paths.py`: Code navigation fault paths the facade tests leave unexercised.

`scripts/code_navigation.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_code_navigation_fault_paths.py -q`.
