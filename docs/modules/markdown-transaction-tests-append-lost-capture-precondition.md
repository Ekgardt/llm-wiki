# Tests of `scripts/markdown_transaction.py`

`tests/test_append_lost_capture_precondition.py` exercise `scripts/markdown_transaction.py` through its public names.

`scripts/markdown_transaction.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_append_lost_capture_precondition.py -q`.
