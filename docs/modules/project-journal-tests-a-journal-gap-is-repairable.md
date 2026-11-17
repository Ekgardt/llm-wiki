# Tests of `scripts/project_journal.py`

- `tests/test_a_journal_gap_is_repairable.py`: A sequence the database calls committed must be in the journal.

`scripts/project_journal.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_journal_gap_is_repairable.py -q`.
