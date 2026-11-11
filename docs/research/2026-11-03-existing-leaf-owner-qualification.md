# Qualify existing leaf owners without moving their production code

Date: 2026-11-03. Boundaries under the approved whole-project mandate;
production files qualified in place.

Source inspection identifies five existing standard-library-only responsibilities:
`okf_types` (page taxonomy and retention defaults), `host_transcripts` (configured
and default transcript roots), `ephemeral_paths` (temporary/job path spellings),
`embedding_model` (pinned model identity and query/passage prefixes), and
`inference_threads` (process-local tracked inference shutdown). These are distinct
responsibilities, not one generic utility owner. Keep production paths and bytes
unchanged; an existing coherent module does not need a cosmetic relocation.

Proposed packets keep exact sources, current qualification/instructions, relevant
historical research and complete pure tests. Existing host-default, ephemeral-root
and inference-thread unit bodies can move out of mixed integration files without
changing assertions. Adapter/retention/MCP consumer tests remain intact integration
obligations. The standalone inference-process exit test can belong to its owner
without moving. Dynamic subprocess imports require explicit source inclusion.

Add guards against product imports and import-time inference start/IO where the
current contract supports them. Do not call every stdlib-only module pure:
inference_threads owns shared process state, locking and atexit registration at
start; ephemeral_paths consults environment and filesystem resolution. Preserve
these effects, synchronization and failure behavior. Do not invent a daemon,
configuration authority, new model version or changed retention policy.

Primary practice checked 2026-11-03, three independent publishers:
- Python threading documentation describes join/event lifecycle and daemon exit
  hazards. The unversioned page now describes Python 3.15; only APIs already in
  the supported 3.10 runtime may be relied upon, verified separately.
  https://docs.python.org/3/library/threading.html
- pytest monkeypatch documents scoped restoration of environment/attributes;
  use it to isolate platform/configuration tests without replacing real consumers.
  https://docs.pytest.org/en/stable/how-to/monkeypatch.html
- Import Linter documents independent/protected/forbidden module contracts;
  guards must make the actual dependency direction executable.
  https://import-linter.readthedocs.io/en/stable/contract_types/

Alternatives rejected: relocate already independent code (unneeded compatibility
work), group by small byte size (unrelated state owners), mock away integration
imports to force packet size (unqualified behavior), change pinned model or host
environment contracts during decomposition (outside the mandate). Fresh built-in
caller/dependency graph, source test inventory, exact-transfer proof, real consumers,
Python 3.10, CCN and packet reconstruction remain required before completion.

Supported-runtime references were checked separately: Python 3.10 threading and
pathlib expose the Event/join and is_relative_to/resolve operations used here.
The docs mark 3.10 unsupported upstream; the repository requirement is preserved,
not described as current upstream support.
https://docs.python.org/3.10/library/threading.html
https://docs.python.org/3.10/library/pathlib.html

The pinned model's own README was reachable through its raw revision URL after
the HTML blob URL failed. Model identity and preprocessing are preserved, not
upgraded or inferred from a generic current model listing:
https://huggingface.co/intfloat/multilingual-e5-small/raw/614241f622f53c4eeff9890bdc4f31cfecc418b3/README.md
Existing eight consumer files passed before any transfer: 57 tests in 3.35 s.


## Qualification evidence

All five production files retain their original SHA-256 hashes. Five existing
unit bodies moved with identical AST; every remaining integration function also
has identical AST. Only now-unused imports and resulting import spacing were
removed from two mixed test files. The normal-process exit test remains at its
original path and is owned by inference-threads. Original history is not edited.

Existing consumer baseline: 57 + 66 tests, also 123 on Python 3.10. Combined
qualification after new guards and unit coverage: 154 passed on Python 3.12 and
154 on 3.10. Shard weighting: 60 passed. Ruff and actual CCN/shape passed for
production owners, all new tests and modified mixed test files. The first Ruff
pass identified residual import spacing after unused imports were removed; the
normal import formatter fixed it without touching function bodies.

The new guards pass already-independent production; no artificial production
failure is claimed. Import coupling is checked statically and in guarded fresh
processes. A separate fresh-process test refuses inference start/atexit work at
import. New behavior coverage includes configuration whitespace/defaults,
deduplication, real symlink containment, OSError resolution fallback, role prefixes,
failed thread start, cleanup and idle settlement. No model is downloaded or run.

Current ownership: 523/3248 files, 2725 unassigned. Qualification of these five
owners does not complete the project or native-platform testing. Evidence:
logs/existing-leaf-production-hashes.json, existing-leaf-test-transfer.json,
existing-leaf-{combined,py310,weights}.txt and existing-leaf-ownership.json.

Global qualification: 1611 passed, 2 platform skips in 149.25 seconds. All 66
packets reconstructed every original path and byte in the first materialized
pass; the five new packets were 57949..61176 tokens before this evidence text.
Final artifacts and exact counts are in logs/existing-leaf-final-bundles.txt and
context-bundle-comparison.json. Full f9016ec7 regression remains in progress;
that older checkpoint does not include this qualification or the compiler fix.
