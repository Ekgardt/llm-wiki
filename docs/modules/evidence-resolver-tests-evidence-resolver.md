# Tests of `scripts/evidence_resolver.py`

`tests/test_evidence_resolver.py` exercise `scripts/evidence_resolver.py` through its public names.

`scripts/evidence_resolver.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_evidence_resolver.py -q`.
