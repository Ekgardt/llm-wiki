# Tests of `scripts/llm_wiki/install_records.py`

- `tests/test_install_records_lock_and_commands_refuse_what_they_cannot_keep.py`

`scripts/llm_wiki/install_records.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_install_records_lock_and_commands_refuse_what_they_cannot_keep.py -q`.
