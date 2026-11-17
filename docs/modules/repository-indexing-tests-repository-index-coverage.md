# Tests of `scripts/repository_indexing.py`

- `tests/test_repository_index_coverage.py`: What a repository index covers, and what it says it covered.

`scripts/repository_indexing.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_repository_index_coverage.py -q`.
