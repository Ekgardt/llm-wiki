# Tests of `scripts/python_series.py`

- `tests/test_an_update_names_the_python_it_needs.py`: An update never lands on code this machine's Python cannot run, and a nightly recovers.

`scripts/python_series.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_update_names_the_python_it_needs.py -q`.
