# Tests of `scripts/evidence_resolver.py`

- `tests/test_live_evidence_reuses_only_a_content_verified_parse.py`: Repeated live claims reuse parsing, while each lookup still reads the source.

`scripts/evidence_resolver.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_live_evidence_reuses_only_a_content_verified_parse.py -q`.
