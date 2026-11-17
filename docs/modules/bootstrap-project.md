# Bootstrap a new project into the vault from its existing git history

`scripts/bootstrap_project.py` and the tests that exercise them most.

When you start tracking a new project, this script auto-generates seed knowledge pages from: - git log (key commits → timeline of decisions) - README.md (project description) - docs/ directory (existing documentation) - Directory structure (architecture overview)

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_bootstrap_publication.py -q`.
