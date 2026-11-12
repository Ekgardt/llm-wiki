# Tests of `scripts/compile_receipts.py`

- `tests/test_the_compile_sees_the_pages_its_day_is_about.py`: A batch is offered the pages its days are about first, not the early ones by path.

`scripts/compile_receipts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_compile_sees_the_pages_its_day_is_about.py -q`.
