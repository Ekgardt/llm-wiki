# Tests of `scripts/corpus_snapshot.py`

- `tests/test_the_default_corpus_budget_is_shared.py`: Physical source count alone does not exhaust the wider existing walk budget.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_default_corpus_budget_is_shared.py -q`.
