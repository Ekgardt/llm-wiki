# Existing path derivation and verified-artifact owners

2026-11-03; baseline6780ef23; approved autonomous decomposition mandate.
Qualify `scripts/lsp_paths.py` and `scripts/verified_artifacts.py` in place, as
separate owners. Both are already stdlib-only leaves; relocating them would add
compatibility work without removing a dependency. Keep production bytes, public
imports, cache schema/path, timestamp rules, eviction policy and every limit.
LSP paths derive names without creating directories. VerifiedArtifacts caches
already established digests/verdicts; it does not establish a digest's truth or
own a generation, graph reader, catalog selection or runtime admission.

Keep the complete lsp_paths focused suite. Move seven pure artifact-cache tests
and their two private setup helpers from the mixed digest/graph/search suite into
`tests/modules/verified_artifacts/test_cache.py`, preserving definition ASTs.
The common `_aged` timestamp helper moves once into the owner test support module,
used by both pure and mixed tests. Keep six mixed graph/search integrations in
the original file. Include the full existing two-test verdict-eviction suite in
the artifact packet and assign it to that owner. No assertion is removed or
copied, and no integration is hidden as an owner test to avoid dependencies.

Research checked against three independent primary publishers today:
- Git's racy-git manual explains stat-cache identity and the timestamp race:
  https://git-scm.com/docs/racy-git
- Python 3.10 pathlib documents concrete path resolution and OS differences:
  https://docs.python.org/3.10/library/pathlib.html
- Microsoft File Times describes filesystem resolution and delayed timestamp
  updates: https://learn.microsoft.com/en-us/windows/win32/sysinfo/file-times
These explain why identity/timestamp behavior must not be simplified during
qualification. They do not prove immunity to hostile in-place file mutation.
Python 3.10 remains supported by this project despite its upstream EOL notice;
no runtime, technology, dependency or filesystem-support change is proposed.

Alternatives: combine path derivation and caching (unrelated ownership), relocate
both under a shared generic utility facade (new coupling), or include the mixed
integration file as if it tested only the leaf (conceals its graph/search needs).
Select existing production boundaries and exact test separation. Higher-level
integration packets remain visible unfinished work in the global ownership map.

Baseline unchanged focused and related integration tests:42 passed. Production
callers inspected directly: install_pyright, pyright_profile/session and profiles
use lsp_paths; generation_catalog, evidence_graph and search_memory use the cache
through lazy imports. Fresh built-in index/graph must be checked before code.
Qualification requires exact moved ASTs and original assertions, import isolation,
full manifest/guide/source coverage, actual serialized round-trip <=100000 tokens,
related integrations, Python 3.10, Ruff and actual complexity/shape checks.

Fresh built-in generation: generation-18e46a0b2cfdb478-31cec721, commit6780ef23,
1843 sources/7139 chunks. Indexed callers: managed_pyright_root20, remembered
artifact digest3, shared timestamp helper17 (name matches checked against source).
The graph is incomplete; direct imports and test bodies supplement it. Evidence:
logs/path-artifact-leaves-graph.json. The same42 baseline checks passed on3.10.

Qualification:48 related checks passed on both Python3.12 and3.10;19 weighted
checks passed; all1086 module/integration guards passed,2 skipped. Ruff and the
actual complexity/shape analyzer pass. All original cache test/helper definition
ASTs are preserved exactly; production sources and unchanged focused suites are
byte-identical. All48 serialized artifacts reconstruct their exact paths/bytes.
Before this evidence paragraph, lsp-paths70209, verified-artifacts77295 tokens;
largest packet96258. Overlapping packet totals3815584 serialized/4175057 raw.
Registry378 of3108 tracked files,2730 unassigned. Whole-project decomposition
and paired task-cycle efficiency remain incomplete. Full6780ef23 runs separately;
no current full pass or native Windows/macOS qualification is claimed.
