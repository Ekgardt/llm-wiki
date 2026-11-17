# Tests of `scripts/breadcrumb_protocol.py`

`tests/test_historical_capture_reuses_only_canonical_record_parsing.py` exercise `scripts/breadcrumb_protocol.py`.

`scripts/breadcrumb_protocol.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_historical_capture_reuses_only_canonical_record_parsing.py -q`.
