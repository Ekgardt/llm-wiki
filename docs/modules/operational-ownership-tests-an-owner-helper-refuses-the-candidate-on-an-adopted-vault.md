# Tests of `scripts/operational_ownership.py`

- `tests/test_an_owner_helper_refuses_the_candidate_on_an_adopted_vault.py`: On an adopted vault an owner helper needs the vault's registry, not the candidate.

`scripts/operational_ownership.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_owner_helper_refuses_the_candidate_on_an_adopted_vault.py -q`.
