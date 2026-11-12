# Tests of `scripts/repository_indexing.py`

- `tests/test_a_gone_top_level_folder_does_not_stop_the_index.py`: A removed top-level folder is a removal, and a new one is named, not silently skipped.

`scripts/repository_indexing.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_gone_top_level_folder_does_not_stop_the_index.py -q`.
