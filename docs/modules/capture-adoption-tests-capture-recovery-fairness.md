# Tests of `scripts/capture_adoption.py`

- `tests/test_capture_recovery_fairness.py`: Missing older records cannot permanently hide a valid later capture.

`scripts/capture_adoption.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_capture_recovery_fairness.py -q`.
