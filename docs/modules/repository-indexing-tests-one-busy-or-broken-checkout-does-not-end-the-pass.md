# Tests of `scripts/repository_indexing.py`

- `tests/test_one_busy_or_broken_checkout_does_not_end_the_pass.py`: One busy or broken checkout is a named row of the nightly pass, not its end.

`scripts/repository_indexing.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_one_busy_or_broken_checkout_does_not_end_the_pass.py -q`.
