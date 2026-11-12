# Tests of `scripts/install_pyright.py`

- `tests/test_every_pinned_download_retries_the_network.py`: A TCP reset during the Pyright download is tried again, from the start (audit 2026-09-27 C-10).

`scripts/install_pyright.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_every_pinned_download_retries_the_network.py -q`.
