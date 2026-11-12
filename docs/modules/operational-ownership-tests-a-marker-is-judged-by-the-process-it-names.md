# Tests of `scripts/operational_ownership.py`

- `tests/test_a_marker_is_judged_by_the_process_it_names.py`: A stored owner is judged by the process it recorded, not by its PID (audit 2026-09-26 C-12).

`scripts/operational_ownership.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_marker_is_judged_by_the_process_it_names.py -q`.
