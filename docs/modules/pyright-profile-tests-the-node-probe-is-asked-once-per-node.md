# Tests of `scripts/pyright_profile.py`

- `tests/test_the_node_probe_is_asked_once_per_node.py`: One `node --version` answer stands for the executable it was taken from.

`scripts/pyright_profile.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_node_probe_is_asked_once_per_node.py -q`.
