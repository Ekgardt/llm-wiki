# User-level SessionStart hook — inject per-project state.md

`scripts/session_start_project_state.py` and the tests that exercise them most.

This hook fires on every Claude Code session start, regardless of cwd. It resolves the current project's slug, reads (or creates) the corresponding `knowledge/projects/<slug>/state.md`, and emits its content as additionalContext so Claude starts the session knowing where we left off in this project.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_project_is_a_directory_the_owner_works_in.py tests/test_a_project_named_con_still_gets_a_handoff.py tests/test_project_context_output_failure.py tests/test_slug.py -q`.
