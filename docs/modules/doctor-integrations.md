# Doctor's checks of agent hook integrations and the Codex hook probe

`scripts/llm_wiki/doctor_integrations.py` and the tests that exercise them most.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_codex_probe_transport.py tests/test_codex_rendered_hooks.py tests/test_doctor_names_hooks_older_than_the_release.py -q`.
