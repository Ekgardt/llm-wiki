# Tests of `scripts/install_control.py`

- `tests/test_ci_and_scheduler_gaps.py`: Every PowerShell script parses, and a systemd pass is stopped above its own worst case.

`scripts/install_control.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_ci_and_scheduler_gaps.py -q`.
