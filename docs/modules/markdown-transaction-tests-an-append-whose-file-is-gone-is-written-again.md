# Tests of `scripts/markdown_transaction.py`

- `tests/test_an_append_whose_file_is_gone_is_written_again.py`: A repeated append answers `committed` only while the file it wrote still exists.

`scripts/markdown_transaction.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_append_whose_file_is_gone_is_written_again.py -q`.
