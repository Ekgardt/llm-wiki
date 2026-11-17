# Tests of `scripts/doctor.py`

`tests/test_a_late_check_is_unfinished_not_broken.py` exercise `scripts/doctor.py`.

`scripts/doctor.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_late_check_is_unfinished_not_broken.py -q`.
