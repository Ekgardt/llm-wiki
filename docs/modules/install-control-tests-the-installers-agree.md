# Tests of `scripts/install_control.py`

- `tests/test_the_installers_agree.py`: The two installers judge the same things the same way, and an unloaded job does not block an uninstall.

`scripts/install_control.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_installers_agree.py -q`.
