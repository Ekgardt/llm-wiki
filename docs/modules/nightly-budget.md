# The nightly pass's steps and how long the slowest possible run of them takes

`scripts/nightly_budget.py` and the tests that exercise them most.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_fix_arrives_when_the_checkout_moves.py tests/test_a_pass_that_knows_how_long_it_can_be.py tests/test_a_prune_inside_its_step.py tests/test_a_step_no_provider_answered_is_not_green.py -q`.
