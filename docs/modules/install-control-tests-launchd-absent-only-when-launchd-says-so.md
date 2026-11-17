# Tests filed with `scripts/install_control.py`

- `tests/test_launchd_absent_only_when_launchd_says_so.py`: A failed `launchctl print` is not an absent job (audit 2026-09-26, regress 11).

These files do not import `scripts/install_control.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/llm_wiki/install_launchd.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_launchd_absent_only_when_launchd_says_so.py -q`.
