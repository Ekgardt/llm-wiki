# CODE-03: generations for repositories other than the vault

`scripts/repository_index.py` and the tests that exercise them most.

Three verbs over the one existing catalog -- `index` a named repository, `list` what is indexed, `detect` what changed since. Nothing here adds a graph, a catalog, a runtime root or a daemon: a foreign repository's generation is an ordinary immutable generation under `cache/evidence-graph/generations/`, disposable derived cache like every other one.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_refused_worktree_waits_for_a_new_commit.py tests/test_repository_refresh_shares_the_callers_budget.py -q`.
