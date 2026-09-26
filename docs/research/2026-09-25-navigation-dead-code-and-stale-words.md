# Navigation: dead code and stale words

Date: 2026-09-25. Audit item C-44 (docs/AUDIT-2026-09-25-full.md).

## Facts (grep and the codebase-memory graph, 2026-09-25)
1. `code_intelligence._require_text` and its chain (`_require_bounded_utf8`,
   `_utf8`, `_require_nfc`) have no caller. `Capability.DECLARATIONS`, `IMPORTS`
   and `INHERITANCE` are named only by two tests. Nothing turns a string into a
   `Capability`, so no request can carry them. The unsupported-capability branch
   in `code_navigation._query_refusal` is reachable only through them.
2. `repository_index.py` (module docstring) and `generation_catalog.py`
   (`_scoped_generation`) say a lost vault pointer sends queries "back to the
   legacy index". That index was retired on 2026-09-23; a search with no active
   generation now reads Markdown directly (`no_active_generation`).
   `retrieval._source_sha256` says the legacy FTS index "stores" no digest.
3. `WorkspaceDelta.configuration_changed` is computed and read by nothing but its
   own tests.
4. `find_dead_code` says `symbol` narrows the question; the live path ignored it
   and answered for the whole repository.
5. `code_graph.py <dir>` (CLAUDE.md: "index code graph") builds no index. It
   prints counts and, through `detect_code_tools`, writes `cache/code_tools.json`
   — read by nothing (trace_path: only `index_directory` and `main` call it) —
   after running `<dir>/node_modules/.bin/tsc --version`, a program the analysed
   repository controls.
6. `workspace_revision._git_output` caps every Git run at 5 s
   (`GIT_STATUS_TIMEOUT_SECONDS`) even when the caller's deadline is longer, so a
   large repository's `git status` fails inside a budget that would have fit.

## Source (fetched 2026-09-25)
Visual Studio Code, "Workspace Trust",
https://code.visualstudio.com/docs/editing/workspaces/workspace-trust: "Workspace
Trust provides an extra layer of security when working with unfamiliar code, by
preventing automatic code execution of any code in your workspace if the
workspace is open in "Restricted Mode"." Running a workspace's own `tsc` to learn
a version nobody reads is exactly the automatic execution that guidance avoids.

## Decisions
1. Remove the dead helpers and the three members; replace the runtime branch by a
   guard test that every `Capability` has a route.
2. Reword the comments to the present contract.
3. Remove `configuration_changed`.
4. The live path returns only the named symbol's candidates; the docstring says
   the reference scan narrows only on the stored path.
5. Remove `detect_code_tools`, its manifest writer and the tool probes;
   `code_graph.py <dir>` prints the live parse counts and writes nothing.
   CLAUDE.md/AGENTS.md name the real indexing command
   (`repository_index.py index <dir>`); docs/STRUCTURE.md drops `code_tools.json`.
   An existing `cache/code_tools.json` is disposable cache, read by nothing.
6. With a caller deadline the Git run uses it; 5 s applies only without one.

## Files
- scripts/code_intelligence.py
- scripts/code_navigation.py
- scripts/repository_index.py
- scripts/generation_catalog.py
- scripts/retrieval.py
- scripts/workspace_revision.py
- scripts/code_graph.py
- CLAUDE.md
- AGENTS.md
- docs/STRUCTURE.md
- tests/test_code_navigation.py
- tests/test_code_graph.py
- tests/test_workspace_revision.py
- tests/test_mcp_server.py
