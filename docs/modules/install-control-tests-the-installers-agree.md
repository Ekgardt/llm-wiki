# Tests filed with `scripts/install_control.py`

- `tests/test_the_installers_agree.py`: The two installers judge the same things the same way, and an unloaded job does not block an uninstall.

These files do not import `scripts/install_control.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/llm_wiki/install_launchd.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_installers_agree.py -q`.
