# Tests of `scripts/fact_keys.py`

- `tests/test_an_entity_page_opens_on_the_second_day_and_is_extended_by_code.py`: A thing gets a page only when it recurs, and the page grows by dated pointer lines, by code.

`scripts/fact_keys.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_entity_page_opens_on_the_second_day_and_is_extended_by_code.py -q`.
