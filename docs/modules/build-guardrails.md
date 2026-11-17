# Guard rails — auto-inject learned corrections to prevent repeating mistakes

`scripts/build_guardrails.py` and the tests that exercise them most.

A correction the user gives is written verbatim to the daily log; the nightly compile turns it into a typed knowledge page. But the agent doesn't SEE that page when working unless it searches for it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_guardrails.py tests/test_the_guard_rails_say_how_many_rules_they_left_out.py -q`.
