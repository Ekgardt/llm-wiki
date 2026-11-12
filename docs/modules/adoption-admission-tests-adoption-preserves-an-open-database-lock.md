# Tests of `scripts/llm_wiki/adoption_admission.py`

- `tests/test_adoption_preserves_an_open_database_lock.py`: An adoption identity check must leave a live SQLite writer exclusive.

`scripts/llm_wiki/adoption_admission.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_adoption_preserves_an_open_database_lock.py -q`.
