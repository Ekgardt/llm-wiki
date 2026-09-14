# A base install can search

Dated 2026-09-14. Item 1.4 of `docs/AUDIT-2026-09-14-2.md`. The research before the fix.

## What was found

- `pyproject.toml` base dependencies are `mcp` and, on 3.10, `tomli`. `pyyaml` is only
  in the `dev` group, and `numpy` only in the `hybrid` extra and `dev`. The installers
  and the nightly update sync with `--no-default-groups` (`install.sh`, `install.ps1`,
  `sync_memory.py`, `self_update.BASELINE_SYNC_COMMAND`), so a base install has
  neither. Neither comes in through `mcp` (walked in `uv.lock` today).
- Module-level third-party imports in `scripts/`, found by parsing every file's top
  level on this date: `yaml` in `corpus_snapshot.py`; `numpy` in `fact_keys.py` and
  `evidence_pruning.py`. Nothing else.
- With `yaml` and `numpy` blocked by an import hook, 20 script modules fail to import
  (run on this machine today): `corpus_snapshot`, `search_memory`, `evidence_graph`,
  `evidence_graph_builder`, `context_compiler`, `knowledge_extractor`,
  `code_navigation`, `pyright_session` and 12 more through `yaml`; `fact_keys` and
  `evidence_pruning` through `numpy`. `query_memory._with_keys_leg` imports
  `fact_keys` before it checks whether a key store exists, so every grounded answer on
  a base install fails on `numpy`; MCP `recall` fails on `yaml`.
- The CI smoke test lists MCP tools and imports no retrieval path.
- The code graph: `corpus_snapshot` is imported by the search, graph, compile-context
  and code-navigation paths; `fact_keys` ← `query_memory._with_keys_leg`;
  `evidence_pruning` ← `query_memory._Pruner.spans_of`.

## Practice on this date

- A dependency a module imports unconditionally is a required dependency; "extras"
  are for features whose code guards its imports
  ([Python Packaging User Guide, dependency specifiers and optional dependencies](https://packaging.python.org/en/latest/specifications/pyproject-toml/#dependencies-optional-dependencies)).
- `numpy` publishes binary wheels for every supported platform and Python version this
  project targets, and PyYAML ships a pure-Python fallback; both are already pinned in
  `uv.lock` for all three Python markers.

## The decision

- `pyyaml` and `numpy` become base dependencies, with the bounds already used in the
  `dev` group and the `hybrid` extra. `uv.lock` is regenerated without network
  (`uv lock --offline`), since both are already resolved there.
- A structure test parses the top level of every script: a third-party module
  imported there must be a base dependency. A new unguarded import of an extra's
  package fails the suite instead of a user's install.

Why not the alternatives:

- **Make every such import lazy.** Three modules today, but the class returns with the
  next top-level import; the test above catches it either way, and frontmatter parsing
  (`yaml`) is core, not optional.

Files: `pyproject.toml`, `uv.lock`, `tests/test_a_base_install_can_search.py`,
`docs/research/2026-09-14-a-base-install-can-search.md`.
