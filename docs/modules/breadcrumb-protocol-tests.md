# Tests of `scripts/breadcrumb_protocol.py`

`tests/test_historical_capture_reuses_only_canonical_record_parsing.py` exercise `scripts/breadcrumb_protocol.py` through its public names.

`scripts/breadcrumb_protocol.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_historical_capture_reuses_only_canonical_record_parsing.py -q`.
