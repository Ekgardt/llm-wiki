# Tests of `scripts/session_start_context.py`

`tests/test_a_missed_nightly_is_caught_up.py` exercise `scripts/session_start_context.py`.

`scripts/session_start_context.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_missed_nightly_is_caught_up.py -q`.
