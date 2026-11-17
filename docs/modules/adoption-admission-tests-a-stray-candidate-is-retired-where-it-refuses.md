# Tests of `scripts/llm_wiki/adoption_admission.py`

- `tests/test_a_stray_candidate_is_retired_where_it_refuses.py`: A provably stray candidate is moved aside where writers are refused.

`scripts/llm_wiki/adoption_admission.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_stray_candidate_is_retired_where_it_refuses.py -q`.
