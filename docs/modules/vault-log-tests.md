# Tests of `scripts/vault_log.py`

- `tests/test_the_vault_log_is_private.py`: The vault log is written to a private file; the tracked `knowledge/log.md` stays the template.

`scripts/vault_log.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_vault_log_is_private.py -q`.
