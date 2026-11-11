# Tests of `scripts/repository_indexing.py`

`tests/test_a_code_generation_is_not_abandoned.py` exercise `scripts/repository_indexing.py` through its public names.

`scripts/repository_indexing.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_code_generation_is_not_abandoned.py -q`.
