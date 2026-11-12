# Tests of `scripts/install_pyright.py`

- `tests/test_install_lock_contention.py`: Two installers starting together must wait, not abort.

`scripts/install_pyright.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_install_lock_contention.py -q`.
