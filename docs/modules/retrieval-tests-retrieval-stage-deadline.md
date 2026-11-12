# Tests of `scripts/retrieval.py`

- `tests/retrieval_stage_deadline.py`: Control only the original retrieval deadline guard in stub-backend tests.

`scripts/retrieval.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/retrieval_stage_deadline.py -q`.
