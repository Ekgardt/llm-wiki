# Tests of `scripts/reflection.py`

`tests/test_automatic_writer_integration.py` exercise `scripts/reflection.py` through its public names.

`scripts/reflection.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_automatic_writer_integration.py -q`.
