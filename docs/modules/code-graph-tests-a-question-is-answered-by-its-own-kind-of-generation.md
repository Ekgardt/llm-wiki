# Tests of `scripts/code_graph.py`

`tests/test_a_question_is_answered_by_its_own_kind_of_generation.py` exercise `scripts/code_graph.py` through its public names.

`scripts/code_graph.py` itself belongs to another module: this packet carries its exact derived description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_question_is_answered_by_its_own_kind_of_generation.py -q`.
