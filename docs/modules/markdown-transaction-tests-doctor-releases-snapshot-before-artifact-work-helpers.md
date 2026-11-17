# Tests of `scripts/markdown_transaction.py`

- `tests/doctor_releases_snapshot_before_artifact_work_helpers.py`: Helpers shared by test_doctor_releases_snapshot_before_artifact_work.py and the test files importing them.

`scripts/markdown_transaction.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/doctor_releases_snapshot_before_artifact_work_helpers.py -q`.
