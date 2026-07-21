# Production Truth Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development to implement this plan task-by-task.
> Every production-code step follows red-green-refactor. Do not commit or push
> unless the user explicitly requests it.

**Goal:** Make the ordinary installed maintenance and MCP paths incapable of
serving evidence from the wrong repository or an incomplete generation, while
restoring workspace-wide code extraction and routing all common context paths
through the grounded compiler boundary.

**Architecture:** Add a closed repository-scope object to v2 generation
manifests and fail closed for repository-scoped readers. Carry one immutable
`CorpusSnapshot` through graph and FTS construction, seal both artifacts before
one catalog CAS activation, and preserve v1/legacy readers only as explicit
fallback. Code extraction runs once per immutable workspace and is partitioned
into per-source records for incremental ownership.

**Tech Stack:** Python 3.10, dataclasses, pathlib, Git identity probes, SQLite
rollback journal/FTS5, existing generation catalog and Evidence Graph, pytest,
Ruff.

**Research:**
`docs/superpowers/specs/2026-07-19-world-class-practices-matrix.md`, especially
Sections 1-4. Primary practices are Git object/worktree identity, immutable
content-addressed publication, SQLite atomic commit, SCIP identity, Tree-sitter,
and evidence-backed negative claims.

---

## Baseline note

The branch starts at integration commit `452f32f`. Ruff passes. The complete
Python 3.10 all-extras suite exceeded the 20-minute command timeout at 72% after
showing six pre-existing failures; Python 3.14 is not a valid baseline because
Windows file-identity tests fail broadly there. Each task must run its focused
tests, and the final gate must rerun the full suite on CPython 3.10 with a long
enough timeout and preserve the exact failure list if baseline issues remain.

## Task 1: Freeze documentation invariants

**Files:**
- Test: `tests/test_structure.py`
- Test: `tests/test_readme_i18n.py`
- Verify: `AGENTS.md`
- Verify: `CLAUDE.md`
- Verify: `docs/STRUCTURE.md`
- Verify: `knowledge/notes/solo-operator-superset-product-decision.md`

- [ ] **Step 1: Add a failing contract test**

Add assertions that the two agent contracts remain byte-identical and contain
the approved superset decision path, and that `docs/STRUCTURE.md` names
repository scope, `search.sqlite3`, and the staged operational databases.

```python
def test_superset_contract_is_canonical(repo_root: Path) -> None:
    agents = (repo_root / "AGENTS.md").read_bytes()
    claude = (repo_root / "CLAUDE.md").read_bytes()
    structure = (repo_root / "docs" / "STRUCTURE.md").read_text(encoding="utf-8")
    assert agents == claude
    assert b"solo-operator-superset-product-decision.md" in agents
    assert "repository-scoped" in structure
    assert "search.sqlite3" in structure
    assert "task-control.sqlite3" in structure
```

- [ ] **Step 2: Run the test and verify RED**

Run: `uv run --python 3.10 pytest tests/test_structure.py -q`

Expected before the test is completed correctly: FAIL on a missing assertion or
fixture name, not an unrelated import error.

- [ ] **Step 3: Make the test use the repository's existing root fixture**

Adapt only the fixture name to existing `test_structure.py` conventions. Do not
change the documented contract merely to satisfy the test.

- [ ] **Step 4: Verify GREEN and lint public knowledge**

Run:

```text
uv run --python 3.10 pytest tests/test_structure.py tests/test_readme_i18n.py -q
uv run --python 3.10 python scripts/lint_memory.py --scope all
```

Expected: PASS and zero new knowledge lint findings.

## Task 2: Canonical repository and checkout scope

**Files:**
- Create: `scripts/repository_scope.py`
- Test: `tests/test_repository_scope.py`
- Modify: `scripts/schemas/evidence-graph-manifest-v1.json`
- Modify: `scripts/generation_catalog.py`
- Test: `tests/test_generation_catalog.py`

- [ ] **Step 1: Write failing identity tests**

Cover:

- same resolved checkout through equivalent paths yields the same ID;
- two roots with the same basename yield different IDs;
- main checkout and Git worktree have the same repository ID but different
  checkout IDs;
- a non-Git directory receives a stable local checkout identity;
- fields are bounded and JSON-serializable.

Desired public contract:

```python
@dataclass(frozen=True)
class RepositoryScope:
    schema_version: str
    repository_id: str
    checkout_id: str
    checkout_root: str
    git_common_dir: str | None
    git_commit: str | None

def resolve_repository_scope(directory: Path) -> RepositoryScope:
    ...
```

- [ ] **Step 2: Verify RED**

Run: `uv run --python 3.10 pytest tests/test_repository_scope.py -q`

Expected: FAIL because `repository_scope` does not exist.

- [ ] **Step 3: Implement deterministic identity**

Use resolved normalized paths and bounded `git -C <root> rev-parse` calls. Hash
the canonical identity payload rather than using `Path.name`. Git failures return
a local non-Git scope; they do not abort code analysis.

```python
def _identity(prefix: str, values: Sequence[str]) -> str:
    payload = "\0".join(values).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(payload).hexdigest()}"
```

Do not use remotes as primary identity because they can be absent or changed.

- [ ] **Step 4: Add the closed manifest object**

Allow an optional `repository_scope` in v1 manifests for migration, with exact
keys matching `RepositoryScope`. Unknown properties, non-canonical hashes,
relative roots, or overlong values fail validation.

- [ ] **Step 5: Verify focused GREEN**

Run:

```text
uv run --python 3.10 pytest tests/test_repository_scope.py tests/test_generation_catalog.py -q
uv run --python 3.10 ruff check scripts/repository_scope.py scripts/generation_catalog.py tests/test_repository_scope.py tests/test_generation_catalog.py
```

Expected: PASS.

## Task 3: Bind code readers to repository scope

**Files:**
- Modify: `scripts/evidence_graph.py`
- Modify: `scripts/code_graph.py`
- Test: `tests/test_evidence_graph_recovery.py`
- Test: `tests/test_code_graph.py`
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the wrong-repository regression**

Create repository A and B with the same basename under different parents. Publish
an active generation bound to A. Point `LLM_WIKI_STATE_ROOT` at the shared state,
then query B through both `find_dead_code` and `get_architecture`.

Assert:

```python
assert report["source_generation"] is None
assert report["fallback"] is True
assert "a_only_symbol" not in json.dumps(report)
```

Also invoke MCP without monkeypatching `code_graph` and assert no A path is
rewritten under B.

- [ ] **Step 2: Verify RED**

Run:

```text
uv run --python 3.10 pytest tests/test_code_graph.py -k "repository_binding" -q
uv run --python 3.10 pytest tests/test_mcp_server.py -k "external_repository" -q
```

Expected: FAIL because the active global generation is consumed.

- [ ] **Step 3: Add a repository-aware opener**

```python
@classmethod
def open_active_for_repository(
    cls,
    catalog: object,
    repository_scope: RepositoryScope,
    *,
    deadline: float | None = None,
) -> "EvidenceGraph | None":
    active = catalog.get_active(deadline=deadline)
    if active is None or active.get("repository_scope") != repository_scope.as_dict():
        return None
    return cls.open_active(catalog, deadline=deadline)
```

Implement comparison through validated canonical objects, not unchecked dictionaries.
Mismatch must not repair or mutate the global active pointer.

- [ ] **Step 4: Guard the common code-graph choke point**

Change `_active_evidence_graph(directory)` to resolve the requested scope and use
`open_active_for_repository`. Keep public MCP and `code_graph` signatures stable.
An unbound legacy generation must take the existing honest live fallback.

- [ ] **Step 5: Verify GREEN**

Run:

```text
uv run --python 3.10 pytest tests/test_evidence_graph_recovery.py tests/test_code_graph.py tests/test_mcp_server.py -q
uv run --python 3.10 ruff check scripts/evidence_graph.py scripts/code_graph.py tests/test_evidence_graph_recovery.py tests/test_code_graph.py tests/test_mcp_server.py
```

Expected: PASS.

## Task 4: Emit repository scope from production maintenance

**Files:**
- Modify: `scripts/evidence_graph_builder.py`
- Modify: `scripts/doctor.py`
- Test: `tests/test_evidence_graph_builder.py`
- Test: `tests/test_generation_maintenance.py`

- [ ] **Step 1: Write failing builder and maintenance tests**

Assert the exact validated `repository_scope` appears in `manifest.json`, the
repository node uses `repository_id` rather than `root.name`, and a parent
generation cannot be incrementally reused under another checkout scope.

- [ ] **Step 2: Verify RED**

Run: `uv run --python 3.10 pytest tests/test_evidence_graph_builder.py tests/test_generation_maintenance.py -k "repository_scope" -q`

Expected: FAIL because the builder has no scope parameter.

- [ ] **Step 3: Thread the immutable scope through build APIs**

Add `repository_scope: RepositoryScope | None` to full and incremental build
contracts. Include its canonical object in `_build_manifest()`. Incremental reuse
requires exact equality with the parent scope; otherwise perform a clean rebuild.

- [ ] **Step 4: Replace basename identity**

In Doctor maintenance, resolve the scope once from `root`, pass it to the builder,
and call code extraction with `repository_id=scope.repository_id`.

- [ ] **Step 5: Verify GREEN**

Run:

```text
uv run --python 3.10 pytest tests/test_evidence_graph_builder.py tests/test_evidence_graph_incremental.py tests/test_generation_maintenance.py -q
uv run --python 3.10 ruff check scripts/evidence_graph_builder.py scripts/doctor.py
```

Expected: PASS.

## Task 5: Classify code by suffix before natural language

**Files:**
- Modify: `scripts/corpus_snapshot.py`
- Test: `tests/test_corpus_snapshot.py`
- Test: `tests/test_generation_maintenance.py`

- [ ] **Step 1: Write the failing suffix matrix**

Parameterize every supported code suffix. At minimum:

```python
@pytest.mark.parametrize(
    ("name", "language"),
    [("app.py", "python"), ("app.ts", "typescript"), ("main.go", "go")],
)
def test_code_language_uses_suffix_before_text(tmp_path, name, language):
    ...
    assert source.record.language == language
```

Also build a `.py` through production maintenance and assert it produces a
Python function node rather than `unsupported_semantics` for language `en`.

- [ ] **Step 2: Verify RED**

Run: `uv run --python 3.10 pytest tests/test_corpus_snapshot.py -k "code_language" -q`

Expected: `.py` is currently `en`, so the test fails.

- [ ] **Step 3: Implement the smallest classifier**

Use a shared immutable suffix map or a small dependency-free helper. Selection
order for code candidates is explicit metadata, supported suffix, then text
language only for unknown text-like code candidates.

```python
language = (
    metadata.get("language")
    or (_code_language(candidate.path) if candidate.kind == "code" else None)
    or _infer_language(text)
)
```

Bump the collector/extractor version so installed defective generations cannot be
reported current.

- [ ] **Step 4: Verify GREEN**

Run:

```text
uv run --python 3.10 pytest tests/test_corpus_snapshot.py tests/test_generation_maintenance.py -k "language or python" -q
uv run --python 3.10 ruff check scripts/corpus_snapshot.py tests/test_corpus_snapshot.py
```

Expected: PASS.

## Task 6: Restore workspace-wide extraction with per-source ownership

**Files:**
- Modify: `scripts/code_extractor.py`
- Modify: `scripts/doctor.py`
- Modify: `scripts/evidence_graph_builder.py`
- Test: `tests/test_code_extractor.py`
- Test: `tests/test_evidence_graph_incremental.py`
- Test: `tests/test_generation_maintenance.py`

- [ ] **Step 1: Write production cross-file tests**

Build `app.py` importing and calling `dep.py` through
`run_generation_maintenance()`. Require resolved `IMPORTS` and `CALLS` edges.
Then add, change, delete, and rename `dep.py`; each incremental result must equal
a clean rebuild and must turn resolved edges into grounded unresolved observations
when appropriate.

- [ ] **Step 2: Verify RED**

Run: `uv run --python 3.10 pytest tests/test_generation_maintenance.py -k "cross_file or unresolved" -q`

Expected: FAIL because production calls `extract_code((captured,), ...)`.

- [ ] **Step 3: Add workspace extraction partitioning**

Run `extract_code()` once for all captured code sources in the immutable snapshot.
Partition records by evidence/source ownership:

- occurrence and evidence belong to their `source_id`;
- assertions/observations belong to their evidence source;
- definitions belong to their definition occurrence;
- cross-file edges belong to the referencing source;
- shared structural nodes use deterministic reference ownership.

Return one `SourceExtraction` per builder callback without returning the complete
workspace graph from every source.

- [ ] **Step 4: Populate dependencies and membership fingerprints**

Include imported modules, resolved targets, inheritance targets, and existing
workspace references in `source_dependencies`. Compute the workspace manifest
from sorted source ID, path, and language membership. Addition/deletion/rename or
language reclassification forces conservative workspace re-resolution.

- [ ] **Step 5: Preserve uncertainty**

Exactly one statically proven target creates an assertion. Zero or multiple
targets remain `missing_dependency`, `unresolved_reference`,
`ambiguous_target`, or `dynamic_dispatch` observations.

- [ ] **Step 6: Verify GREEN and equivalence**

Run:

```text
uv run --python 3.10 pytest tests/test_code_extractor.py tests/test_evidence_graph_incremental.py tests/test_generation_maintenance.py -q
uv run --python 3.10 ruff check scripts/code_extractor.py scripts/evidence_graph_builder.py scripts/doctor.py
```

Expected: PASS.

## Task 7: Define a complete v2 generation

**Files:**
- Modify: `scripts/generation_catalog.py`
- Modify: `scripts/schemas/evidence-graph-manifest-v1.json`
- Modify: `scripts/search_memory.py`
- Test: `tests/test_generation_catalog.py`
- Test: `tests/test_search_ranking.py`

- [ ] **Step 1: Write failing v2 artifact tests**

A `corpus-generation/v2` manifest must contain exactly one
`source-manifest.json`, `evidence.sqlite3`, and `search.sqlite3`. Missing search,
bad FTS metadata, duplicate chunks, wrong tokenizer, or a recomputed artifact hash
over semantically invalid data must fail registration.

- [ ] **Step 2: Verify RED**

Run: `uv run --python 3.10 pytest tests/test_generation_catalog.py -k "v2 or search" -q`

Expected: FAIL because catalog validates only declared artifacts.

- [ ] **Step 3: Make FTS validation public and bounded**

Extract `validate_generation_fts_artifact(...)` from the existing read-time
validator. Add deadline/cancellation checks, row bounds, immutable read-only open,
and exact metadata comparison.

- [ ] **Step 4: Add v2 catalog dispatch**

After generic hash checks, v2 validation requires the complete artifact set and
calls both Evidence Graph and FTS semantic validators. Keep v1 readable during
migration.

- [ ] **Step 5: Verify GREEN**

Run:

```text
uv run --python 3.10 pytest tests/test_generation_catalog.py tests/test_search_ranking.py -q
uv run --python 3.10 ruff check scripts/generation_catalog.py scripts/search_memory.py
```

Expected: PASS.

## Task 8: Build graph and FTS from one immutable snapshot

**Files:**
- Modify: `scripts/search_memory.py`
- Modify: `scripts/evidence_graph_builder.py`
- Modify: `scripts/doctor.py`
- Test: `tests/test_generation_integration.py`
- Test: `tests/test_generation_maintenance.py`

- [ ] **Step 1: Write the production maintenance regression**

Run maintenance, inspect the active manifest, and search a unique term. Assert
`search.sqlite3` exists, shares the source-manifest hash, reports the new
generation ID, and never calls legacy retrieval.

- [ ] **Step 2: Write failure-preserves-prior tests**

With a valid prior generation active, inject FTS failure and source drift before
publication. Assert the prior generation remains active and the candidate is not
registered or recoverable.

- [ ] **Step 3: Verify RED**

Run: `uv run --python 3.10 pytest tests/test_generation_maintenance.py -k "consumable or fts_failure or source_drift" -q`

Expected: FAIL because maintenance builds graph only.

- [ ] **Step 4: Bound and harden FTS construction**

Extend `build_generation_fts()` with deadline/cancellation, SQLite progress
handler, temporary cleanup, file fsync, and generation-directory fsync.

- [ ] **Step 5: Carry `CorpusSnapshot` through the builder**

Separate corpus extractor provenance from graph extractor provenance. Build FTS
after graph construction but before `manifest.json`; include all artifact
descriptors in one sorted manifest.

- [ ] **Step 6: Reuse one publication fence**

Extract the existing writer-gate plus `validate_live_snapshot()` boundary from
`search_memory.publish_generation()`. Use it around register+activate for both
publication paths.

- [ ] **Step 7: Fix maintenance current detection**

Matching source hash is insufficient. Return `current` only for a complete,
semantically valid v2 generation bound to the requested repository scope.

- [ ] **Step 8: Verify GREEN**

Run:

```text
uv run --python 3.10 pytest tests/test_generation_integration.py tests/test_generation_maintenance.py tests/test_evidence_graph_builder.py tests/test_evidence_graph_incremental.py -q
uv run --python 3.10 ruff check scripts/search_memory.py scripts/evidence_graph_builder.py scripts/doctor.py
```

Expected: PASS.

## Task 9: Report and recover complete-generation health

**Files:**
- Modify: `scripts/doctor.py`
- Modify: `scripts/scheduled_nightly.py`
- Modify: `scripts/sync_memory.py`
- Test: `tests/test_generation_maintenance.py`
- Test: `tests/test_scheduled_nightly.py`
- Test: `tests/test_sync_memory.py`

- [ ] **Step 1: Write health and recovery tests**

Assert graph-only v1 is degraded/repairable, complete v2 is healthy, corrupt v2
is error, matching-hash incomplete v2 is rebuilt, and incomplete v2 orphans are
never activated.

- [ ] **Step 2: Verify RED**

Run: `uv run --python 3.10 pytest tests/test_generation_maintenance.py -k "search_index or graph_only or incomplete" -q`

- [ ] **Step 3: Implement truthful fields**

Add `search_index`, `search_schema`, and `search_integrity` to Doctor generation
details. Keep legacy index health separate. Nightly and sync report generation
failure without mislabeling legacy-index success as complete-generation success.

- [ ] **Step 4: Verify GREEN**

Run:

```text
uv run --python 3.10 pytest tests/test_generation_maintenance.py tests/test_scheduled_nightly.py tests/test_sync_memory.py -q
uv run --python 3.10 ruff check scripts/doctor.py scripts/scheduled_nightly.py scripts/sync_memory.py
```

Expected: PASS.

## Task 10: Expose grounded QA through existing MCP recall

**Files:**
- Modify: `scripts/mcp_server.py`
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing schema and dispatch tests**

Keep exactly 12 tools. Add optional recall arguments `grounded: boolean` and
`profile: enum(QA_PROFILES)`. Profile without `grounded=true` is invalid. Default
recall remains backward-compatible search.

- [ ] **Step 2: Verify RED**

Run: `uv run --python 3.10 pytest tests/test_mcp_server.py -k "grounded" -q`

- [ ] **Step 3: Implement minimal dispatch**

Call `query_memory.grounded_qa()` directly with the MCP operation deadline. Do not
use the CLI string wrapper. Return the verified answer inside normal envelope
`data`; abstention is a valid partial answer, not a protocol error.

- [ ] **Step 4: Verify GREEN**

Run: `uv run --python 3.10 pytest tests/test_mcp_server.py tests/test_grounded_qa.py -q`

Expected: PASS and exactly 12 tools.

## Task 11: Make Context Compiler the SessionStart packing boundary

**Files:**
- Modify: `scripts/context_compiler.py`
- Modify: `scripts/session_start_context.py`
- Modify: `scripts/integration_adapter.py`
- Modify: `scripts/build_context.py`
- Test: `tests/test_context_compiler.py`
- Test: `tests/test_context_noise.py`

- [ ] **Step 1: Write failing compiler-boundary tests**

Spy on `compile_context_items()` from direct SessionStart, integration adapter,
and generated project context. Assert mandatory health and handoff survive whole,
optional history drops under pressure, and no item is sliced.

- [ ] **Step 2: Verify RED**

Run: `uv run --python 3.10 pytest tests/test_context_compiler.py tests/test_context_noise.py -k "compiler or handoff" -q`

- [ ] **Step 3: Add one thin compiler facade**

```python
def compile_context_items(
    items: Iterable[ContextItem],
    *,
    budget: ContextBudget = DEFAULT_CONTEXT_BUDGET,
    **packing: object,
) -> PackedContext:
    return pack_context(items, budget, **packing)
```

Use the facade internally and at all SessionStart final-packing call sites. Do not
duplicate packer behavior.

- [ ] **Step 4: Correct semantic classes**

Map safety, health, handoff, evidence, and history by section meaning. Advisory is
optional `handoff`; recovered project handoff remains mandatory.

- [ ] **Step 5: Verify GREEN**

Run:

```text
uv run --python 3.10 pytest tests/test_context_compiler.py tests/test_context_noise.py tests/test_integration_injection.py -q
uv run --python 3.10 ruff check scripts/context_compiler.py scripts/session_start_context.py scripts/integration_adapter.py scripts/build_context.py
```

Expected: PASS.

## Task 12: Make installers trust process status

**Files:**
- Modify: `install.ps1`
- Modify: `install.sh`
- Test: `tests/test_integration_injection.py`
- Test: `tests/test_quality_guards.py`

- [ ] **Step 1: Write failing fake-uv tests**

Exit 1 with output `3081 passed` must warn. Exit 0 with output containing `failed`
must succeed. Assert neither installer pattern-matches pytest output to determine
status.

- [ ] **Step 2: Verify RED**

Run: `uv run --python 3.10 pytest tests/test_integration_injection.py tests/test_quality_guards.py -k "pytest_exit" -q`

- [ ] **Step 3: Capture exit status immediately**

PowerShell stores `$LASTEXITCODE` immediately after `uv`. Bash runs pytest inside
an `if` assignment and captures `$?` without `|| true`. Preserve current
warn-and-continue installation policy.

- [ ] **Step 4: Verify GREEN**

Run: `uv run --python 3.10 pytest tests/test_integration_injection.py tests/test_quality_guards.py -q`

Expected: PASS.

## Task 13: Phase verification and review

**Files:**
- Verify all modified files
- Update current-state sections in `docs/ARCHITECTURE.md`, `docs/STRUCTURE.md`,
  `docs/USER-GUIDE.md`, and the roadmap only after behavior exists

- [ ] **Step 1: Run focused subsystem suites**

```text
uv run --python 3.10 pytest tests/test_repository_scope.py tests/test_generation_catalog.py tests/test_evidence_graph_builder.py tests/test_evidence_graph_incremental.py tests/test_evidence_graph_recovery.py tests/test_generation_integration.py tests/test_generation_maintenance.py tests/test_corpus_snapshot.py tests/test_code_extractor.py tests/test_code_graph.py tests/test_mcp_server.py tests/test_grounded_qa.py tests/test_context_compiler.py tests/test_context_noise.py tests/test_integration_injection.py tests/test_quality_guards.py tests/test_scheduled_nightly.py tests/test_sync_memory.py -q
```

- [ ] **Step 2: Run static and structural verification**

```text
uv run --python 3.10 ruff check scripts/ tests/
uv run --python 3.10 python scripts/lint_memory.py --scope all
uv run --python 3.10 pytest tests/test_structure.py tests/test_readme_i18n.py -q
git diff --check
```

- [ ] **Step 3: Run the complete canonical-runtime suite**

Run: `uv run --python 3.10 pytest -q`

Expected: all supported tests pass. If a baseline failure remains, preserve the
exact command/output and do not call the phase complete.

- [ ] **Step 4: Run two-stage review**

First review exact compliance with the product decision and this plan. Only after
spec approval, review code quality, security, migration, and degraded behavior.
Resolve every finding and rerun affected tests.

- [ ] **Step 5: Report without overclaiming**

Report implemented behavior, test evidence, remaining baseline failures, and the
next roadmap phase. Do not claim competitor superiority; Phase 1 establishes the
truth boundary, not competitive proof.
