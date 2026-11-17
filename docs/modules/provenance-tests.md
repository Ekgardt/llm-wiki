# Tests of `scripts/provenance.py`

- `tests/test_intent_conditional_trust.py`: The curated-pages-first prior applies to knowledge questions only.

`scripts/provenance.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_intent_conditional_trust.py -q`.
