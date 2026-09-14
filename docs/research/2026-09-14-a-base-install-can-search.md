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

Decided by me on the owner's delegation («решения принимай самостоятельно после
выполнения пяти правил», 2026-09-14), after `4b80c55` was reverted for breaking the
recorded contract:

- The contract "the production install carries no PyYAML"
  (`tests/test_structure.py::_assert_pyyaml_stays_a_dev_dependency`) was introduced in
  `9026d19` — the same commit that made `corpus_snapshot` import `yaml` at module level
  for the canonical frontmatter reader. No research note records a reason for it. It
  has never been compatible with a working search on a production install.
- A hand-written frontmatter parser instead of PyYAML would change how stored
  metadata is read (flow lists, aliases, quoting — `tests/test_context_compiler.py`
  pins a flow alias list) and so the rows every generation stores; item 0.2 of the same
  audit showed what a change in reading does to stored identity. PyYAML is small, ships
  a pure-Python fallback, and `safe_load` is the reader the code already trusts.
- So **`pyyaml` becomes a base dependency**; the structure test asserts that instead.
  `uv.lock` is regenerated offline (already resolved).
- **`numpy` stays in the `hybrid` profile**, as `test_hybrid_and_dev_profiles_own_their_imports`
  records. `fact_keys` and `evidence_pruning` import it inside the functions that
  compute with vectors, which only run when an encoder or a key store exists — both
  come with the hybrid profile. The modules themselves, and the grounded-answer path
  that imports them, load without it.
- A structure test parses the top level of every script: a third-party module
  imported there must be a base dependency.

Files: `pyproject.toml`, `uv.lock`, `scripts/fact_keys.py`, `scripts/evidence_pruning.py`,
`tests/test_structure.py`, `tests/test_quality_guards.py`, `tests/test_a_base_install_can_search.py`,
`docs/research/2026-09-14-a-base-install-can-search.md`.
