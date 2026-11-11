# Tests of `scripts/repository_indexing.py`

- `tests/test_one_entry_does_not_refuse_a_repository.py`: One odd entry under a code root is skipped by name, not a refusal of the whole repository.

`scripts/repository_indexing.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_one_entry_does_not_refuse_a_repository.py -q`.
