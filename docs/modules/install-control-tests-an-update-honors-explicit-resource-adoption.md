# Tests of `scripts/install_control.py`

- `tests/test_an_update_honors_explicit_resource_adoption.py`: Explicit takeover during an update retains the reviewed current baseline.

`scripts/install_control.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_update_honors_explicit_resource_adoption.py -q`.
