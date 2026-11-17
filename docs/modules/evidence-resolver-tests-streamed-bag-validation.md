# Tests of `scripts/evidence_resolver.py`

- `tests/test_streamed_bag_validation.py`: Archive validation owns its stream and retains metadata, not full daily bytes.

`scripts/evidence_resolver.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_streamed_bag_validation.py -q`.
