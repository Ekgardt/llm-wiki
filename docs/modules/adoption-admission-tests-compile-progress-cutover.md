# Tests of `scripts/llm_wiki/adoption_admission.py`

- `tests/test_compile_progress_cutover.py`: Offline cutover retains a real backup and resumes interrupted schema work.

`scripts/llm_wiki/adoption_admission.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_progress_cutover.py -q`.
