# Retire the repository generations no checkout will read again (#24, D1)

`scripts/repository_retention.py` and the tests that exercise them most.

A foreign repository's generation is registered and never activated, so the vault's pruner (`prune_generations.py`) reports each one as pending and removes none: every refresh added a generation and nothing took one away, and a worktree removed by its agent left all of its generations behind. This pass decides per checkout, by identity, never by age:

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_repository_retention.py -q`.
