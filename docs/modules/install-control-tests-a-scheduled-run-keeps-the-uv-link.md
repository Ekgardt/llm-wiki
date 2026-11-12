# Tests of `scripts/install_control.py`

- `tests/test_a_scheduled_run_keeps_the_uv_link.py`: A scheduled run calls uv by the path the operator's PATH gives, links kept.

`scripts/install_control.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_scheduled_run_keeps_the_uv_link.py -q`.
