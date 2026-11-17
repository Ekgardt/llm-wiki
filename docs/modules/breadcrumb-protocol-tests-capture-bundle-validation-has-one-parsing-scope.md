# Tests of `scripts/breadcrumb_protocol.py`

- `tests/test_capture_bundle_validation_has_one_parsing_scope.py`: Canonical parsing reuse never substitutes for physical capture authority.

`scripts/breadcrumb_protocol.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_capture_bundle_validation_has_one_parsing_scope.py -q`.
