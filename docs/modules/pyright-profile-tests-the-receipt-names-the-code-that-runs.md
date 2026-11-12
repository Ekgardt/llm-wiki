# Tests of `scripts/pyright_profile.py`

- `tests/test_the_receipt_names_the_code_that_runs.py`: Audit 3, B20: the install receipt attests the code, not only the loader.

`scripts/pyright_profile.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_receipt_names_the_code_that_runs.py -q`.
