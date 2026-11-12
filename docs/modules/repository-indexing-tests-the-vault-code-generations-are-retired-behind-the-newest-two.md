# Tests of `scripts/repository_indexing.py`

- `tests/test_the_vault_code_generations_are_retired_behind_the_newest_two.py`: The vault's code generations are retired behind the newest two; its memory is left alone.

`scripts/repository_indexing.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_vault_code_generations_are_retired_behind_the_newest_two.py -q`.
