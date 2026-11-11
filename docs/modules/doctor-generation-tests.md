# Tests of `scripts/llm_wiki/doctor_generation.py`

- `tests/test_a_blind_check_says_why_it_is_blind.py`: A check that could not read the state must say how far past the bound it was.
- `tests/test_an_answer_says_how_old_its_index_is.py`: An answer says how old its index is, and the index follows the compile.

`scripts/llm_wiki/doctor_generation.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_blind_check_says_why_it_is_blind.py tests/test_an_answer_says_how_old_its_index_is.py -q`.
