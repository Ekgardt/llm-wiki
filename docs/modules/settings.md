# The vault settings reader and registry of limits with their declared basis

`scripts/settings.py` and the tests that exercise them most.

Law 9: a limit that depends on operating conditions is stated with its basis and, where it can be, set by the operator rather than hidden in the code. Every such limit is declared here once, with its default, unit, lower bound and one line of reason, and code reads it only through `setting_value`.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_compile_asked_through_mcp_takes_the_compile_lock.py tests/test_every_limit_states_its_reason.py tests/test_generation_windows_follow_operator_settings.py -q`.
