# Auto-generate per-project context summary for SessionStart injection

`scripts/build_context.py` and the tests that exercise them most.

Reads all knowledge pages tagged with `project: <slug>` in their frontmatter, plus recent daily-log breadcrumbs for that slug, plus the project's state.md handoff note. Produces a compact markdown block that gets injected at SessionStart so the agent immediately knows: what decisions were made, what patterns are known, what gotchas exist, what's currently open — for THIS specific project.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
