# Tests of `scripts/aggregation_pass.py`

- `tests/test_a_counting_question_opens_the_pass_itself.py`: A counting question opens the counting pass by its own shape, and the count counts things.
- `tests/test_a_failed_second_look_keeps_the_first_answer.py`: A second look that fails leaves the verified first answer standing.
- `tests/test_a_wider_fetch_is_judged_by_what_is_new.py`: A wider fetch that brings one new page is used, however large the pool already is.

`scripts/aggregation_pass.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_counting_question_opens_the_pass_itself.py tests/test_a_failed_second_look_keeps_the_first_answer.py tests/test_a_wider_fetch_is_judged_by_what_is_new.py -q`.
