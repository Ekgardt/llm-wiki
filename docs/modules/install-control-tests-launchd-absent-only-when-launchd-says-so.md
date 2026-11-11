# Tests of `scripts/install_control.py`

- `tests/test_launchd_absent_only_when_launchd_says_so.py`: A failed `launchctl print` is not an absent job (audit 2026-09-26, regress 11).

`scripts/install_control.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_launchd_absent_only_when_launchd_says_so.py -q`.
