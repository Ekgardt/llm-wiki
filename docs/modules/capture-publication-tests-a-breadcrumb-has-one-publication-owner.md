# Tests of `scripts/capture_publication.py`

- `tests/test_a_breadcrumb_has_one_publication_owner.py`: Complete breadcrumb acceptance and registration share one fenced capture owner.

`scripts/capture_publication.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_breadcrumb_has_one_publication_owner.py -q`.
