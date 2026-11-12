# Tests of `scripts/llm_wiki/adoption_admission.py`

- `tests/test_the_adoption_gate_names_its_cause.py`: The adoption gate says what it saw, and the queue waits out a busy database.

`scripts/llm_wiki/adoption_admission.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_adoption_gate_names_its_cause.py -q`.
