# Tests of `scripts/workspace_revision.py`

- `tests/test_a_directory_link_inside_the_checkout_does_not_refuse_the_revision.py`: A directory link that stays inside the checkout is passed by; one that leaves it is refused.

`scripts/workspace_revision.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_directory_link_inside_the_checkout_does_not_refuse_the_revision.py -q`.
