# Tests of `scripts/reflection.py`

- `tests/test_a_page_the_rewriter_cannot_read_is_not_a_candidate.py`: The finder and the rewriter of the reflection pass read a page the same way.

`scripts/reflection.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_page_the_rewriter_cannot_read_is_not_a_candidate.py -q`.
