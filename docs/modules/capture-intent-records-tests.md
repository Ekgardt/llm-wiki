# Tests of `scripts/capture_intent_records.py`

`tests/test_breadcrumb_redrive.py` exercise `scripts/capture_intent_records.py` through its public names.

`scripts/capture_intent_records.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_breadcrumb_redrive.py -q`.
