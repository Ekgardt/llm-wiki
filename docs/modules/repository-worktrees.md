# Indexes that follow a repository's worktrees (#24, section D1)

`scripts/repository_worktrees.py` and the tests that exercise them most.

Several agents open a worktree per task; the index must exist without anyone running an indexer. A *registered* repository is one with at least one generation; every other worktree of it that has none is indexed here, with the code roots of its newest sibling, under the same per-repository fence a refresh takes. The nightly `refresh-all` does it for every registered repository, and the MCP server does it once per checkout when a structural answer finds no generation for a worktree whose repository is registered.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
