# Tests of `scripts/co_activation.py`

- `tests/test_a_readme_beside_the_daily_logs_is_not_pending_work.py`: `knowledge/daily/README.md` is not a daily log for any reader of that directory.

`scripts/co_activation.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_readme_beside_the_daily_logs_is_not_pending_work.py -q`.
