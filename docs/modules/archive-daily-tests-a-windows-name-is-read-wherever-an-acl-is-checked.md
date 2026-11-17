# Tests of `scripts/archive_daily.py`

- `tests/test_a_windows_name_is_read_wherever_an_acl_is_checked.py`: Every reader of an `icacls` listing reads it under the owner's code page.

`scripts/archive_daily.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_windows_name_is_read_wherever_an_acl_is_checked.py -q`.
