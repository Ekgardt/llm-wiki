# Tests of `scripts/archive_daily.py`

- `tests/test_streamed_archive_writer.py`: Archive source scopes must finish before publication and intentional deletion.

`scripts/archive_daily.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_streamed_archive_writer.py -q`.
