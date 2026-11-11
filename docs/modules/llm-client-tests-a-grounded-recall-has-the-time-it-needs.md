# Tests of `scripts/llm_client.py`

- `tests/test_a_grounded_recall_has_the_time_it_needs.py`: A grounded recall gets the time a provider needs, and its provider call ends with it.

`scripts/llm_client.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_grounded_recall_has_the_time_it_needs.py -q`.
