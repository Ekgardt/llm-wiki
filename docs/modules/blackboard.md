# Agent coordination blackboard — shared state for parallel agents

`scripts/blackboard.py` and the tests that exercise them most.

When multiple agents work in the same project simultaneously, they need a shared "blackboard" to coordinate: claim tasks, signal completion, leave notes for each other, and detect conflicts.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/blackboard_helpers.py tests/test_blackboard.py tests/test_blackboard_inputs_use_the_journal_contract.py tests/test_blackboard_release_retry.py tests/test_security_invariants.py -q`.
