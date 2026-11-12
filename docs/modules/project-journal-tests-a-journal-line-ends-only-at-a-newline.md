# Tests of `scripts/project_journal.py`

- `tests/test_a_journal_line_ends_only_at_a_newline.py`: A Unicode line separator inside a checkpoint's text does not wedge the journal.

`scripts/project_journal.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_journal_line_ends_only_at_a_newline.py -q`.
