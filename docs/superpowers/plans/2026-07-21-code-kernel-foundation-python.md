# Code Kernel Foundation and Python Vertical Slice Implementation Plan

> **Superseded after Task 5:** Tasks 1-5 were completed and remain valid. On
> 2026-07-22 the user approved the read-only LSP navigation design in
> `docs/superpowers/specs/2026-07-22-read-only-lsp-navigation-design.md`.
> Tasks 6-16 below are retained as historical planning evidence and must not be
> executed. A replacement implementation plan will cover the owned Python/Pyright
> LSP runtime.

> **Historical worker instruction:** This plan must no longer be executed. Its
> unchecked boxes are preserved as historical task text; repository commits and
> the superseding notice above establish that Tasks 1-5 completed before the
> remaining work was replaced.

**Goal:** Build Plan A of the persistent code-intelligence kernel: a repository-scoped Python vertical slice with consent-gated precise analysis, lease-free native syntax fallback, recoverable job ownership, complete freshness identity, and honest coverage published in Evidence Graph v3 through the existing 12 MCP tools.

**Architecture:** Keep `corpus-generation/v2`, `cache/evidence-graph/catalog.sqlite3`, and its single repository-scoped active pointer as the publication boundary. Legacy graph-builder calls default safely to Graph v2; precise code publication passes Graph v3 and verified batches explicitly, while an in-process native syntax fallback can publish v3 without consent or a subprocess. Precise analyzer subprocesses receive a sealed captured workspace under `run/analyzer-runs/<filesystem-run-id>/input/workspace`, never the live checkout; every precise or native indexing job has a process-instance owner heartbeat so crashes are diagnosable and recoverable without weakening explicit consent.

**Tech Stack:** Python 3.10-compatible standard library, CPython `ast` and `symtable`, SQLite rollback-journal with `synchronous=FULL` and no WAL, SCIP v0.9 decoded JSON, LSP 3.18 snapshot payloads, Git, pytest, Ruff, the existing corpus-generation catalog, Evidence Graph, Context Compiler, doctor, and MCP server.

---

## Verified Current-Practice Constraints

Verified on 2026-07-21 and binding on this plan:

- SCIP's current schema gives each `Document` its own `position_encoding`, supports UTF-8, UTF-16, and UTF-32 code-unit offsets, prefers typed single/multi-line ranges, and permits the deprecated exact three/four-integer range only as fallback: <https://github.com/scip-code/scip/blob/main/scip.proto>.
- LSP 3.18 negotiates UTF-8, UTF-16, or UTF-32 positions; UTF-16 is the default only when initialization omits a negotiated value: <https://microsoft.github.io/language-server-protocol/specifications/lsp/3.18/specification/#positionEncodingKind>.
- CPython 3.10 AST columns are UTF-8 byte offsets and `symtable` exposes compiler symbol tables. The AST grammar can change between Python releases, so the exact interpreter is identity-bearing: <https://docs.python.org/3.10/library/ast.html> and <https://docs.python.org/3.10/library/symtable.html>.
- Python `stat_result` exposes nanosecond timestamps, size, mode, device, and inode where supported, but availability and semantics vary by platform. This plan uses persisted stat/directory metadata only to select conservative hash candidates; content SHA-256 remains the freshness proof: <https://docs.python.org/3.10/library/os.html#os.stat_result>.
- The generation manifest uses JSON Schema 2020-12 and closes objects with `additionalProperties: false`; `code_capture` must therefore be added to the schema before builders emit it rather than treated as an undeclared annotation: <https://json-schema.org/draft/2020-12/json-schema-core.html>.
- Windows Job Objects manage process trees and enforce only configured limits; assignment and nested-job behavior must be checked. POSIX `resource` limits are platform-specific and unavailable controls must not be advertised: <https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects> and <https://docs.python.org/3.10/library/resource.html>.
- Windows `CREATE_SUSPENDED` keeps the primary thread from running until `ResumeThread`; use it so Job Object configuration and assignment happen before analyzer entry: <https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags>.
- Python 3.10 warns that `preexec_fn` can deadlock in threaded programs. POSIX limits therefore run in a small trusted launcher process which calls `setrlimit`, `setsid`, and `execve`, never in `Popen(preexec_fn=...)`: <https://docs.python.org/3.10/library/subprocess.html#subprocess.Popen>.
- SQLite `PRAGMA user_version` is application-managed. Schema creation and `user_version` change belong in one explicit write transaction, followed by a read-back check. `BEGIN IMMEDIATE` serializes writers in rollback mode: <https://www.sqlite.org/pragma.html#pragma_user_version> and <https://www.sqlite.org/lang_transaction.html>.
- SQLite transactions are atomic per connection/database transaction; this plan does not claim one transaction spans the consent and catalog databases. Publication uses an application-level repository lock and safe ordering around two separate transactions: <https://www.sqlite.org/lang_transaction.html> and <https://www.sqlite.org/atomiccommit.html>.
- Python documents that process and thread identifiers may be recycled. Analyzer ownership therefore uses `owner_pid` plus a random process-start nonce, and lease expiry remains authoritative rather than treating PID existence alone as identity: <https://docs.python.org/3.10/library/os.html#os.getpid>, <https://docs.python.org/3.10/library/threading.html#threading.get_native_id>, and <https://docs.python.org/3.10/library/secrets.html#secrets.token_hex>.
- Python recommends signaling and joining non-daemon threads for graceful cleanup. The heartbeat worker uses `threading.Event`, a separate short-lived SQLite connection per refresh, and an explicit stop/join boundary: <https://docs.python.org/3.10/library/threading.html#threading.Event>.
- Pytest loads modules named by root `conftest.py` through `pytest_plugins`; because `tests/` is a package, the stable plugin name is `tests.code_kernel_helpers`: <https://docs.pytest.org/en/stable/how-to/writing_plugins.html#requiring-loading-plugins-in-a-test-module-or-conftest-file>.

## File Map

**Create:**

- `knowledge/notes/persistent-code-intelligence-kernel-decision.md`: approved target architecture, clearly separate from current implementation.
- `tests/code_kernel_helpers.py`: shared fixture helpers, introduced without forward dependencies and extended only in the task that introduces each corresponding production API.
- `tests/fixtures/code_kernel/python/`: fixed multi-file Python repository.
- `tests/fixtures/code_kernel/scip-python-v0.9.json`: strict decoded SCIP fixture with typed and legacy ranges.
- `tests/fixtures/code_kernel/scip-valid-output-v0.9.json`: one standalone valid analyzer output used by orchestration subprocess tests.
- `scripts/code_intelligence.py`: closed normalized run, scope, coverage, claim, diagnostic, freshness, and receipt contracts.
- `scripts/code_workspace.py`: bounded external capture and sealed analyzer workspace creation/verification.
- `scripts/code_consent.py`: exact invocation-derived repository consent and start leases.
- `scripts/code_runner.py`: qualified one-shot subprocess execution and verified receipt creation.
- `scripts/code_posix_launcher.py`: trusted POSIX `setrlimit`/`setsid` then `execve` launcher; never imports repository code.
- `scripts/scip_ingest.py`: bounded SCIP v0.9 normalization.
- `scripts/lsp_snapshot.py`: completed one-shot LSP payload normalization; no daemon lifecycle.
- `scripts/python_analyzer.py`: interpreter-identity-bearing CPython AST plus symbol-table analyzer.
- `scripts/code_orchestrator.py`: the sole supported capture-to-publication API.
- `scripts/code_index.py`: non-mutating, repository-scoped, store-first queries.
- `benchmark/code-kernel-python-v1.json`, `benchmark/code-kernel-report-v1.schema.json`, `benchmark/run_code_kernel.py`: deterministic correctness metrics and optional qualified latency reporting.
- `docs/CODE-KERNEL.md`: implemented Plan A behavior after all code tasks pass.

**Modify:**

- `.gitignore`, reciprocal decision pages, `knowledge/index.md`, `knowledge/log.md`, `docs/STRUCTURE.md`, `AGENTS.md`, `CLAUDE.md`, and `tests/test_structure.py`.
- `scripts/evidence_graph.py`, `scripts/evidence_graph_builder.py`, `scripts/generation_catalog.py`, and `scripts/schemas/evidence-graph-manifest-v1.json`.
- `scripts/corpus_snapshot.py`, `scripts/code_extractor.py`, `scripts/code_graph.py`, `scripts/impact_analysis.py`, `scripts/context_compiler.py`, `scripts/mcp_server.py`, `scripts/mcp_contract.py`, and `scripts/doctor.py`.
- `tests/test_corpus_snapshot.py`, `tests/test_code_graph.py`, `tests/test_impact_analysis.py`, `tests/test_context_compiler.py`, `tests/test_mcp_server.py`, `tests/test_mcp_contract.py`, and `tests/test_doctor.py`.
- `.github/workflows/tests.yml`, `tests/README.md`, and `benchmark/COMPARATIVE.md`.

### Task 1: Record The Approved Target Without Claiming It Is Implemented

**Files:**
- Create: `knowledge/notes/persistent-code-intelligence-kernel-decision.md`
- Modify: `.gitignore:33-75`
- Modify: `knowledge/notes/solo-operator-superset-product-decision.md:116-133`
- Modify: `knowledge/notes/derived-evidence-generation-decision.md:73-84`
- Modify: `knowledge/index.md:16-25`
- Modify: `knowledge/log.md:18-24`
- Modify: `docs/STRUCTURE.md:117-155,213-308`
- Modify: `AGENTS.md:35-90`
- Modify: `CLAUDE.md:35-90`
- Modify: `tests/test_structure.py:150-182,323-503`

- [ ] **Step 1: Write the failing target-vs-current structure test**

```python
def test_code_kernel_target_is_approved_but_not_reported_as_current() -> None:
    structure = (ROOT / "docs/STRUCTURE.md").read_text(encoding="utf-8")
    current = structure.split("## Implemented corpus-generation checkpoint", 1)[1].split("\n## ", 1)[0]
    target = structure.split("## Approved code-kernel target", 1)[1].split("\n## ", 1)[0]
    decision_path = ROOT / "knowledge/notes/persistent-code-intelligence-kernel-decision.md"

    assert decision_path.is_file()
    _required_frontmatter_scalars(
        decision_path.read_text(encoding="utf-8"),
        {"type": "decision", "status": "active", "confidence": "high",
         "source_authority": "user", "date": "2026-07-21"},
    )
    assert "evidence-graph/v2" in current
    assert "evidence-graph/v3" not in current
    for value in (
        "evidence-graph/v3", "v2 generations remain readable",
        "run/code-analysis-consent.sqlite3", "run/analyzer-runs/<filesystem-run-id>/",
        "exactly 12 task-shaped tools", "Python 3.10", "approved target",
    ):
        assert value in target
    assert (ROOT / "AGENTS.md").read_bytes() == (ROOT / "CLAUDE.md").read_bytes()
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `uv run pytest tests/test_structure.py::test_code_kernel_target_is_approved_but_not_reported_as_current -q`

Expected: FAIL because the target decision and target section do not exist.

- [ ] **Step 3: Add the public decision, reciprocal links, index, log, and allowlist**

The decision page must use the exact frontmatter above and include these exact statements under `## Decision`:

```markdown
This page records an approved target, not implemented behavior. The current
checkpoint remains `corpus-generation/v2` with `evidence-graph/v2` until the
implementation and verification tasks in the Plan A implementation plan pass.

The approved target is `evidence-graph/v3` inside the existing generation,
with v2 generations remaining readable for structural capabilities. It adds no
second graph, catalog, active pointer, runtime root, persistent daemon, or MCP
tool. It preserves exactly 12 task-shaped tools and Python 3.10 support.

Precise analyzer execution requires repository/analyzer/exact-invocation consent
in `run/code-analysis-consent.sqlite3`. Sealed analyzer scratch state uses
`run/analyzer-runs/<filesystem-run-id>/`. Operational SQLite remains
rollback-journal, `synchronous=FULL`, and no WAL.
```

Add `[[persistent-code-intelligence-kernel-decision]]` to both related decision pages, a `## Decisions` index entry, one append-only 2026-07-21 log entry, and the literal `.gitignore` allowlist line.

- [ ] **Step 4: Add an `## Approved code-kernel target` section without editing the implemented checkpoint**

Document target paths and contracts in `docs/STRUCTURE.md` and both byte-identical agent contracts. Do not add v3 files or behavior to the “Implemented corpus-generation checkpoint” block in this task.

- [ ] **Step 5: Verify no new memory-lint findings against the recorded baseline**

The recorded review baseline is 60 non-blocking findings: 58 `missing_backlinks` findings plus the existing `orphan_daily_logs` and `stale_compiled` findings. Run the same CI gate, which permits only those debt categories plus the repository's already-allowed `missing_sources_section` and `temporal_validity` categories:

Run: `uv run python scripts/lint_memory.py --scope all --fail-on-findings --allowed-categories orphan_daily_logs missing_backlinks missing_sources_section temporal_validity stale_compiled`

Expected: PASS. Then run plain lint for visibility:

Run: `uv run python scripts/lint_memory.py --scope all`

Expected: 60 findings or fewer, with no finding naming `persistent-code-intelligence-kernel-decision.md`. Do not require plain lint to exit zero.

- [ ] **Step 6: Run structure tests and verify GREEN**

Run: `uv run pytest tests/test_structure.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 1**

```bash
git add .gitignore AGENTS.md CLAUDE.md docs/STRUCTURE.md tests/test_structure.py knowledge/index.md knowledge/log.md knowledge/notes/persistent-code-intelligence-kernel-decision.md knowledge/notes/solo-operator-superset-product-decision.md knowledge/notes/derived-evidence-generation-decision.md
git commit -m "docs: record approved code kernel target"
```

### Task 2: Add One Exact Shared Fixture And Helper Contract

**Files:**
- Create: `tests/code_kernel_helpers.py`
- Create: `tests/test_code_kernel_helpers.py`
- Modify: `tests/conftest.py:14-68`
- Create: `tests/fixtures/code_kernel/python/pyproject.toml`
- Create: `tests/fixtures/code_kernel/python/uv.lock`
- Create: `tests/fixtures/code_kernel/python/pkg/__init__.py`
- Create: `tests/fixtures/code_kernel/python/pkg/api.py`
- Create: `tests/fixtures/code_kernel/python/pkg/base.py`
- Create: `tests/fixtures/code_kernel/python/pkg/service.py`
- Create: `tests/fixtures/code_kernel/python/pkg/dynamic.py`
- Create: `tests/fixtures/code_kernel/python/pkg/broken.py`
- Create: `tests/fixtures/code_kernel/python/tests/test_service.py`

- [ ] **Step 1: Write the failing helper round-trip test**

```python
def test_fixture_copy_is_git_scoped_and_deterministic(tmp_path: Path) -> None:
    first = create_python_repository(tmp_path / "first")
    second = create_python_repository(tmp_path / "second")
    assert fixture_digest(first) == fixture_digest(second)
    assert resolve_repository_scope(first).repository_id != resolve_repository_scope(second).repository_id
    assert (first / "pkg/api.py").read_text(encoding="utf-8").startswith("class PublicApi")


def test_shared_plugin_provides_independent_state_and_repository_fixtures(
    state_root: Path, repository: Path, pytestconfig,
) -> None:
    assert state_root.is_dir()
    assert (repository / ".git").exists()
    assert state_root not in repository.parents
    assert pytestconfig.pluginmanager.hasplugin("tests.code_kernel_helpers")
```

- [ ] **Step 2: Run the helper test and verify RED**

Run: `uv run pytest tests/test_code_kernel_helpers.py -q`

Expected: FAIL because `tests/code_kernel_helpers.py` does not exist.

- [ ] **Step 3: Implement the shared helper module used verbatim by later tasks**

```python
FIXTURE_ROOT = Path(__file__).parent / "fixtures/code_kernel/python"


def create_python_repository(destination: Path) -> Path:
    shutil.copytree(FIXTURE_ROOT, destination)
    environment = sanitized_git_environment()
    subprocess.run(["git", "init"], cwd=destination, env=environment, check=True,
                   stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "fixture@example.test"],
                   cwd=destination, env=environment, check=True, capture_output=True, timeout=10)
    subprocess.run(["git", "config", "user.name", "Code Kernel Fixture"],
                   cwd=destination, env=environment, check=True, capture_output=True, timeout=10)
    subprocess.run(["git", "add", "."], cwd=destination, env=environment, check=True,
                   capture_output=True, timeout=10)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=destination,
                   env=environment, check=True, capture_output=True, timeout=10)
    return destination


def fixture_digest(root: Path) -> str:
    values = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.parts
    }
    return hashlib.sha256(canonical_json_bytes(values)).hexdigest()


def source_bytes(snapshot: CorpusSnapshot, source_id: str) -> bytes:
    matches = [source.content for source in snapshot.sources if source.record.logical_id == source_id]
    if len(matches) != 1:
        raise KeyError(source_id)
    return matches[0]


def source_by_path(snapshot: CorpusSnapshot, relative_path: str) -> CapturedSource:
    matches = [
        source for source in snapshot.sources
        if source.record.relative_path == relative_path
    ]
    if len(matches) != 1:
        raise KeyError(relative_path)
    return matches[0]


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    validate_state_root(root)
    return root


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return create_python_repository(tmp_path / "repository")
```

Register the helper module from `tests/conftest.py` so every later test receives the fixtures without local imports:

```python
pytest_plugins = ("tests.code_kernel_helpers",)
```

Task 2 defines only `create_python_repository`, `fixture_digest`, `source_by_path`, `source_bytes`, `state_root`, and `repository`. It must not import contracts or Graph APIs introduced by later tasks. Tasks 3, 4, and 10 extend this module only after introducing the production types used by their helpers.

- [ ] **Step 4: Run helper tests and existing repository-scope tests and verify GREEN**

Run: `uv run pytest tests/test_code_kernel_helpers.py tests/test_repository_scope.py -q`

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add tests/conftest.py tests/code_kernel_helpers.py tests/test_code_kernel_helpers.py tests/fixtures/code_kernel/python
git commit -m "test: add shared code kernel fixtures"
```

### Task 3: Define Complete Normalized Identity, Scope, Claim, And Receipt Contracts

**Files:**
- Create: `scripts/code_intelligence.py`
- Create: `tests/test_code_intelligence.py`
- Modify: `tests/code_kernel_helpers.py`

- [ ] **Step 1: Write failing contract tests**

```python
def test_capability_enum_covers_plan_a_navigation() -> None:
    assert {item.value for item in Capability} == {
        "definitions", "declarations", "references", "calls", "imports", "types",
        "type_definitions", "inheritance", "implementations", "diagnostics",
    }


def test_analysis_identity_is_complete_and_change_sensitive() -> None:
    identity = AnalysisIdentity.create(
        source_manifest_sha256="0" * 64, manifest_sha256="1" * 64,
        lockfile_sha256="2" * 64, sdk_sha256="3" * 64,
        target_sha256="4" * 64, configuration_sha256="5" * 64,
        feature_sha256="6" * 64, invocation_sha256="7" * 64,
        environment_sha256="8" * 64, dependency_state_sha256="9" * 64,
        position_encoding=PositionEncoding.UTF8,
    )
    assert set(identity.as_dict()) == {
        "source_manifest_sha256", "manifest_sha256", "lockfile_sha256", "sdk_sha256",
        "target_sha256", "configuration_sha256", "feature_sha256",
        "invocation_sha256", "environment_sha256",
        "dependency_state_sha256", "position_encoding", "analysis_sha256",
    }
    assert replace(identity, lockfile_sha256="f" * 64).recompute_analysis_sha256() != identity.analysis_sha256


def test_closed_world_requires_complete_expected_scope() -> None:
    scope = AnalysisScope(
        scope_id="scope:" + "1" * 64, run_id="run:" + "2" * 64,
        source_manifest_sha256="3" * 64,
        build_target="default", build_configuration="default",
        expected_source_ids=("source:a", "source:b"), generated_sources="available",
        dependency_resolution="complete", analyzer_support="complete",
    )
    coverage = (Coverage(
        scope_id=scope.scope_id, source_id="source:a",
        capability=Capability.REFERENCES, status=CoverageStatus.COMPLETE,
        closed_world_eligible=True, reason=None,
    ),)
    assert closed_world(scope, coverage, Capability.REFERENCES) is False
```

- [ ] **Step 2: Run the contract tests and verify RED**

Run: `uv run pytest tests/test_code_intelligence.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'code_intelligence'`.

- [ ] **Step 3: Implement closed enums and complete immutable records**

```python
class Capability(str, Enum):
    DEFINITIONS = "definitions"
    DECLARATIONS = "declarations"
    REFERENCES = "references"
    CALLS = "calls"
    IMPORTS = "imports"
    TYPES = "types"
    TYPE_DEFINITIONS = "type_definitions"
    INHERITANCE = "inheritance"
    IMPLEMENTATIONS = "implementations"
    DIAGNOSTICS = "diagnostics"


class PositionEncoding(str, Enum):
    UTF8 = "utf-8"
    UTF16 = "utf-16"
    UTF32 = "utf-32"


@dataclass(frozen=True, slots=True)
class AnalysisIdentity:
    source_manifest_sha256: str
    manifest_sha256: str
    lockfile_sha256: str
    sdk_sha256: str
    target_sha256: str
    configuration_sha256: str
    feature_sha256: str
    invocation_sha256: str
    environment_sha256: str
    dependency_state_sha256: str
    position_encoding: PositionEncoding
    analysis_sha256: str

    @classmethod
    def create(
        cls,
        *,
        source_manifest_sha256: str,
        manifest_sha256: str,
        lockfile_sha256: str,
        sdk_sha256: str,
        target_sha256: str,
        configuration_sha256: str,
        feature_sha256: str,
        invocation_sha256: str,
        environment_sha256: str,
        dependency_state_sha256: str,
        position_encoding: PositionEncoding,
    ) -> AnalysisIdentity:
        """Validate components and compute analysis_sha256 from canonical JSON."""


@dataclass(frozen=True, slots=True)
class AnalysisScope:
    scope_id: str
    run_id: str
    source_manifest_sha256: str
    build_target: str
    build_configuration: str
    expected_source_ids: tuple[str, ...]
    generated_sources: Literal["available", "unavailable", "not-required"]
    dependency_resolution: Literal["complete", "partial", "unavailable"]
    analyzer_support: Literal["complete", "partial", "unsupported", "unqualified"]


@dataclass(frozen=True, slots=True)
class PositionRange:
    byte_start: int
    byte_end: int

    def require_nonempty(self, label: str) -> PositionRange:
        if self.byte_end <= self.byte_start:
            raise ValueError(f"{label} must use a non-empty half-open byte range")
        return self


class CoverageStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNSUPPORTED = "unsupported"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class Coverage:
    scope_id: str
    source_id: str
    capability: Capability
    status: CoverageStatus
    closed_world_eligible: bool
    reason: str | None


_VERIFIED_BATCH_MINT = object()


@dataclass(frozen=True, slots=True, init=False)
class VerifiedAnalysisBatch:
    analysis: NormalizedAnalysis
    analysis_mode: Literal["precise", "native-syntax"]
    source_manifest_sha256: str
    analysis_sha256: str
    receipt_sha256: str | None
    consent_grant_id: str | None
    consent_revision: int | None
    lease_id: str | None

    def __init__(
        self,
        analysis: NormalizedAnalysis,
        *,
        analysis_mode: Literal["precise", "native-syntax"],
        source_manifest_sha256: str,
        analysis_sha256: str,
        receipt_sha256: str | None,
        consent_grant_id: str | None,
        consent_revision: int | None,
        lease_id: str | None,
        _mint: object,
    ) -> None:
        if _mint is not _VERIFIED_BATCH_MINT:
            raise TypeError("VerifiedAnalysisBatch is created by internal verifiers only")
        object.__setattr__(self, "analysis", analysis)
        object.__setattr__(self, "analysis_mode", analysis_mode)
        object.__setattr__(self, "source_manifest_sha256", source_manifest_sha256)
        object.__setattr__(self, "analysis_sha256", analysis_sha256)
        object.__setattr__(self, "receipt_sha256", receipt_sha256)
        object.__setattr__(self, "consent_grant_id", consent_grant_id)
        object.__setattr__(self, "consent_revision", consent_revision)
        object.__setattr__(self, "lease_id", lease_id)


def verify_native_analysis(
    snapshot: CorpusSnapshot,
    analysis: NormalizedAnalysis,
) -> VerifiedAnalysisBatch:
    if analysis.run.identity.source_manifest_sha256 != snapshot.corpus_sha256:
        raise ValueError("native analysis does not match captured source manifest")
    if any(item.evidence_level != EvidenceLevel.SYNTAX for item in analysis.all_claims()):
        raise ValueError("native verification accepts syntax evidence only")
    return VerifiedAnalysisBatch(
        analysis,
        analysis_mode="native-syntax",
        source_manifest_sha256=analysis.run.identity.source_manifest_sha256,
        analysis_sha256=analysis.run.identity.analysis_sha256,
        receipt_sha256=None,
        consent_grant_id=None,
        consent_revision=None,
        lease_id=None,
        _mint=_VERIFIED_BATCH_MINT,
    )
```

Define `NormalizedAnalysis` before `VerifiedAnalysisBatch` in the real module. Add frozen `AnalysisRun`, `SymbolIdentity`, `SymbolClaim`, `RelationshipClaim`, `Diagnostic`, `RelatedLocation`, `Validity`, `AnalyzerReceipt`, and `NormalizedAnalysis`. `AnalysisRun`, `AnalysisScope`, `AnalyzerReceipt`, and `VerifiedAnalysisBatch` all expose the exact field name `source_manifest_sha256`; constructors require it to equal `AnalysisIdentity.source_manifest_sha256`. `AnalyzerReceipt` and `VerifiedAnalysisBatch` also expose `analysis_sha256` and require it to equal `AnalysisIdentity.analysis_sha256`. `AnalysisRun` includes `analysis_mode: Literal["precise", "native-syntax"]`; its receipt/consent invariants match `VerifiedAnalysisBatch`. `RelationshipClaim` has `relation`, `capability`, source identity, optional target identity, optional target text, and `resolution` in `resolved|unresolved|ambiguous`; unresolved claims are not v2 assertions. `Diagnostic` owns sorted `related: tuple[RelatedLocation, ...]`. `Validity` supports symbol, relationship, and diagnostic subjects and current/soft-stale/hard-stale with a stale reason. `VerifiedAnalysisBatch` is an internal construction invariant against accidental API bypass, not a cryptographic boundary against arbitrary in-process Python code.

Extend `tests/code_kernel_helpers.py` here with `make_analysis_scope(snapshot)`, `make_analysis_identity(snapshot, scope)`, `make_run(snapshot, outcome="complete")`, and `make_normalized_analysis(snapshot, scope)` using these now-defined records; `make_run` derives its default scope and identity. Add direct helper round-trip assertions to `tests/test_code_intelligence.py`, including `pytest.raises(TypeError)` when ordinary test code invokes `VerifiedAnalysisBatch(..., _mint=object())`.

- [ ] **Step 4: Implement honest closed-world evaluation**

```python
def closed_world(
    scope: AnalysisScope,
    coverage: Sequence[Coverage],
    capability: Capability,
) -> bool:
    rows = {
        row.source_id: row for row in coverage
        if row.scope_id == scope.scope_id and row.capability == capability
    }
    return (
        scope.generated_sources in {"available", "not-required"}
        and scope.dependency_resolution == "complete"
        and scope.analyzer_support == "complete"
        and set(rows) == set(scope.expected_source_ids)
        and all(row.status in {CoverageStatus.COMPLETE, CoverageStatus.EXCLUDED} for row in rows.values())
        and all(row.closed_world_eligible for row in rows.values())
    )
```

- [ ] **Step 5: Run contract tests and Ruff and verify GREEN**

Run: `uv run pytest tests/test_code_intelligence.py tests/test_code_kernel_helpers.py -q`

Expected: PASS.

Run: `uv run ruff check scripts/code_intelligence.py tests/test_code_intelligence.py tests/code_kernel_helpers.py`

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

```bash
git add scripts/code_intelligence.py tests/test_code_intelligence.py tests/code_kernel_helpers.py
git commit -m "feat: define complete code intelligence contracts"
```

### Task 4: Add Explicit Graph v3 Selection With An Exact Relationship Schema

**Files:**
- Modify: `scripts/evidence_graph.py:21-278,434-775,962-end`
- Modify: `scripts/evidence_graph_builder.py:44-79,196-254,395-700`
- Modify: `scripts/generation_catalog.py:93-120,561-760`
- Modify: `scripts/schemas/evidence-graph-manifest-v1.json:9-18,79-104`
- Modify: `tests/test_evidence_graph.py`
- Modify: `tests/test_evidence_graph_builder.py`
- Modify: `tests/test_evidence_graph_recovery.py`
- Modify: `tests/test_generation_catalog.py`
- Modify: `tests/code_kernel_helpers.py`

- [ ] **Step 1: Write failing explicit-schema tests**

```python
def test_database_schema_is_explicit_and_returned(tmp_path: Path) -> None:
    records = basic_graph_records()
    v2 = create_generation_database(tmp_path / "v2.sqlite3", schema=GraphSchema.V2, **records)
    verified = verify_native_analysis(
        snapshot_for_records(records), make_normalized_analysis_for_records(records)
    )
    v3 = create_generation_database(
        tmp_path / "v3.sqlite3", schema=GraphSchema.V3,
        verified_analyses=(verified,), **records,
    )
    assert (v2, v3) == (GraphSchema.V2, GraphSchema.V3)
    assert sqlite_user_version(tmp_path / "v2.sqlite3") == 2
    assert sqlite_user_version(tmp_path / "v3.sqlite3") == 3


def test_omitted_database_schema_remains_v2_for_legacy_callers(tmp_path: Path) -> None:
    records = basic_graph_records()
    selected = create_generation_database(tmp_path / "legacy.sqlite3", **records)
    assert selected == GraphSchema.V2
    assert sqlite_user_version(tmp_path / "legacy.sqlite3") == 2


def test_builder_manifest_matches_database_schema_for_each_mode(tmp_path: Path) -> None:
    v2 = build_fixture_generation(tmp_path, generation_id="v2", graph_schema=GraphSchema.V2)
    v3 = build_fixture_generation(tmp_path, generation_id="v3", graph_schema=GraphSchema.V3)
    assert v2.manifest["graph_schema_version"] == "evidence-graph/v2"
    assert v3.manifest["graph_schema_version"] == "evidence-graph/v3"


def test_omitted_builder_schema_keeps_legacy_v2_manifest(tmp_path: Path) -> None:
    result = build_fixture_generation(tmp_path, generation_id="legacy")
    assert result.manifest["graph_schema_version"] == "evidence-graph/v2"
    assert sqlite_user_version(result.generation_path / "evidence.sqlite3") == 2


def test_v3_has_normalized_relationships_without_unresolved_v2_assertions(tmp_path: Path) -> None:
    graph = open_v3_fixture(tmp_path)
    assert graph._database.execute("SELECT count(*) FROM relationship_claim").fetchone()[0] > 0
    assert graph._database.execute(
        "SELECT count(*) FROM assertion WHERE resolution != 'resolved'"
    ).fetchone()[0] == 0
    assert graph._database.execute(
        "SELECT count(*) FROM analysis_scope s JOIN analyzer_run r USING (run_id) "
        "WHERE s.source_manifest_sha256 != r.source_manifest_sha256"
    ).fetchone()[0] == 0
```

- [ ] **Step 2: Run Graph tests and verify RED**

Run: `uv run pytest tests/test_evidence_graph.py tests/test_evidence_graph_builder.py tests/test_generation_catalog.py -q -k "explicit or manifest_matches or normalized_relationships"`

Expected: FAIL because schema selection is implicit and v3 tables do not exist.

- [ ] **Step 3: Define explicit schema selection and exact v3 extension**

```python
class GraphSchema(str, Enum):
    V2 = "evidence-graph/v2"
    V3 = "evidence-graph/v3"


def create_generation_database(
    database_path: Path,
    *,
    schema: GraphSchema = GraphSchema.V2,
    sources: Iterable[Mapping[str, object]],
    source_bytes: Mapping[str, bytes],
    nodes: Iterable[Mapping[str, object]],
    occurrences: Iterable[Mapping[str, object]],
    assertions: Iterable[Mapping[str, object]],
    evidence: Iterable[Mapping[str, object]],
    observations: Iterable[Mapping[str, object]],
    dependencies: Iterable[Mapping[str, object]],
    verified_analyses: Iterable[VerifiedAnalysisBatch] = (),
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> GraphSchema:
    """Create one immutable database using only the explicitly selected schema."""
```

The default exists only for compatibility and always means V2. V2 rejects non-empty `verified_analyses`; any non-empty verified batch requires the caller to pass `schema=GraphSchema.V3` explicitly. V3 executes the unchanged v2 schema plus the following exact extension in the same `BEGIN IMMEDIATE` transaction. `create_generation_database` is the low-level sealed-database writer: it accepts captured source records/bytes and only `VerifiedAnalysisBatch` values for v3, but it does not accept `code_capture` and does not build or validate a generation manifest. The publication layer owns that metadata. `VerifiedAnalysisBatch` is an internal constructor-capability invariant introduced in Task 3, not a cryptographic boundary against code running in this Python process. `scripts/evidence_graph.py` exposes no raw-analysis, raw-record, test token, or alternate DDL writer; tests construct syntax batches through `verify_native_analysis` and call the same production writer.

```sql
CREATE TABLE analyzer_run (
  run_id TEXT PRIMARY KEY,
  analysis_mode TEXT NOT NULL CHECK (analysis_mode IN ('precise','native-syntax')),
  repository_id TEXT NOT NULL,
  checkout_id TEXT NOT NULL,
  source_generation_id TEXT NOT NULL,
  source_manifest_sha256 TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  lockfile_sha256 TEXT NOT NULL,
  sdk_sha256 TEXT NOT NULL,
  target_sha256 TEXT NOT NULL,
  configuration_sha256 TEXT NOT NULL,
  feature_sha256 TEXT NOT NULL,
  invocation_sha256 TEXT NOT NULL,
  environment_sha256 TEXT NOT NULL,
  dependency_state_sha256 TEXT NOT NULL,
  analysis_sha256 TEXT NOT NULL,
  position_encoding TEXT NOT NULL CHECK (position_encoding IN ('utf-8','utf-16','utf-32')),
  analyzer_family TEXT NOT NULL,
  analyzer_version TEXT NOT NULL,
  protocol TEXT NOT NULL CHECK (protocol IN ('scip','lsp','native')),
  protocol_version TEXT NOT NULL,
  executable_sha256 TEXT NOT NULL,
  declared_capability_count INTEGER NOT NULL CHECK (declared_capability_count > 0),
  declared_capabilities_sha256 TEXT NOT NULL,
  expected_scope_count INTEGER NOT NULL CHECK (expected_scope_count > 0),
  expected_scope_set_sha256 TEXT NOT NULL,
  receipt_sha256 TEXT,
  receipt_output_sha256 TEXT,
  consent_grant_id TEXT,
  consent_revision INTEGER,
  lease_id TEXT,
  publication_generation_id TEXT NOT NULL,
  publication_expected_active TEXT,
  evidence_level TEXT NOT NULL CHECK (evidence_level IN ('compiler','semantic','syntax','lexical')),
  qualified INTEGER NOT NULL CHECK (qualified IN (0,1)),
  outcome TEXT NOT NULL CHECK (outcome IN
    ('complete','partial','failed','cancelled','rejected','superseded')),
  started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL,
  UNIQUE (run_id, source_manifest_sha256),
  CHECK (
    (analysis_mode='precise' AND evidence_level IN ('compiler','semantic')
      AND receipt_sha256 IS NOT NULL AND receipt_output_sha256 IS NOT NULL
      AND consent_grant_id IS NOT NULL AND consent_revision IS NOT NULL
      AND consent_revision >= 1 AND lease_id IS NOT NULL)
    OR
    (analysis_mode='native-syntax' AND evidence_level IN ('syntax','lexical')
      AND receipt_sha256 IS NULL AND receipt_output_sha256 IS NULL
      AND consent_grant_id IS NULL AND consent_revision IS NULL AND lease_id IS NULL)
  )
) WITHOUT ROWID;

CREATE TABLE run_capability (
  run_id TEXT NOT NULL REFERENCES analyzer_run(run_id),
  capability TEXT NOT NULL CHECK (capability IN
    ('definitions','declarations','references','calls','imports','types',
     'type_definitions','inheritance','implementations','diagnostics')),
  PRIMARY KEY (run_id, capability)
) WITHOUT ROWID;

CREATE TABLE analysis_scope (
  scope_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  source_manifest_sha256 TEXT NOT NULL,
  build_target TEXT NOT NULL,
  build_configuration TEXT NOT NULL,
  expected_source_count INTEGER NOT NULL CHECK (expected_source_count >= 0),
  expected_source_set_sha256 TEXT NOT NULL,
  generated_sources TEXT NOT NULL CHECK (generated_sources IN
    ('available','unavailable','not-required')),
  dependency_resolution TEXT NOT NULL CHECK (dependency_resolution IN
    ('complete','partial','unavailable')),
  analyzer_support TEXT NOT NULL CHECK (analyzer_support IN
    ('complete','partial','unsupported','unqualified')),
  FOREIGN KEY (run_id, source_manifest_sha256)
    REFERENCES analyzer_run(run_id, source_manifest_sha256),
  UNIQUE (run_id, build_target, build_configuration),
  UNIQUE (scope_id, run_id)
) WITHOUT ROWID;

CREATE TABLE expected_source (
  scope_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  source_id TEXT NOT NULL REFERENCES source(source_id),
  source_sha256 TEXT NOT NULL,
  disposition TEXT NOT NULL CHECK (disposition IN ('included','excluded','generated')),
  PRIMARY KEY (scope_id, source_id),
  FOREIGN KEY (scope_id, run_id) REFERENCES analysis_scope(scope_id, run_id)
) WITHOUT ROWID;

CREATE TABLE coverage (
  scope_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  capability TEXT NOT NULL CHECK (capability IN
    ('definitions','declarations','references','calls','imports','types',
     'type_definitions','inheritance','implementations','diagnostics')),
  status TEXT NOT NULL CHECK (status IN
    ('complete','partial','failed','cancelled','rejected','unsupported','excluded')),
  closed_world_eligible INTEGER NOT NULL CHECK (closed_world_eligible IN (0,1)),
  reason TEXT,
  PRIMARY KEY (scope_id, source_id, capability),
  FOREIGN KEY (scope_id, run_id) REFERENCES analysis_scope(scope_id, run_id),
  FOREIGN KEY (scope_id, source_id) REFERENCES expected_source(scope_id, source_id),
  FOREIGN KEY (run_id, capability) REFERENCES run_capability(run_id, capability),
  CHECK ((status IN ('complete','excluded') AND reason IS NULL)
      OR (status NOT IN ('complete','excluded') AND reason IS NOT NULL)),
  CHECK (closed_world_eligible = 0 OR status IN ('complete','excluded'))
) WITHOUT ROWID;

CREATE TABLE symbol_claim (
  claim_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  capability TEXT NOT NULL CHECK (capability IN ('definitions','declarations')),
  identity_scheme TEXT NOT NULL,
  identity_value TEXT NOT NULL,
  display_name TEXT NOT NULL,
  symbol_kind TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('definition','declaration')),
  byte_start INTEGER NOT NULL CHECK (byte_start >= 0),
  byte_end INTEGER NOT NULL CHECK (byte_end > byte_start),
  evidence_level TEXT NOT NULL CHECK (evidence_level IN ('compiler','semantic','syntax','lexical')),
  ambiguity INTEGER NOT NULL CHECK (ambiguity IN (0,1)),
  FOREIGN KEY (scope_id, run_id) REFERENCES analysis_scope(scope_id, run_id),
  FOREIGN KEY (scope_id, source_id) REFERENCES expected_source(scope_id, source_id),
  FOREIGN KEY (run_id, capability) REFERENCES run_capability(run_id, capability),
  CHECK ((capability='definitions' AND role='definition')
      OR (capability='declarations' AND role='declaration'))
) WITHOUT ROWID;

CREATE TABLE relationship_claim (
  claim_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_identity_scheme TEXT NOT NULL,
  source_identity_value TEXT NOT NULL,
  relation TEXT NOT NULL CHECK (relation IN
    ('REFERENCES_SYMBOL','CALLS','IMPORTS','HAS_TYPE',
     'TYPE_DEFINITION','INHERITS','IMPLEMENTS')),
  capability TEXT NOT NULL CHECK (capability IN
    ('references','calls','imports','types','type_definitions','inheritance','implementations')),
  target_identity_scheme TEXT,
  target_identity_value TEXT,
  target_text TEXT,
  resolution TEXT NOT NULL CHECK (resolution IN ('resolved','unresolved','ambiguous')),
  byte_start INTEGER NOT NULL CHECK (byte_start >= 0),
  byte_end INTEGER NOT NULL CHECK (byte_end > byte_start),
  evidence_level TEXT NOT NULL CHECK (evidence_level IN ('compiler','semantic','syntax','lexical')),
  ambiguity INTEGER NOT NULL CHECK (ambiguity IN (0,1)),
  FOREIGN KEY (scope_id, run_id) REFERENCES analysis_scope(scope_id, run_id),
  FOREIGN KEY (scope_id, source_id) REFERENCES expected_source(scope_id, source_id),
  FOREIGN KEY (run_id, capability) REFERENCES run_capability(run_id, capability),
  CHECK ((resolution='resolved' AND target_identity_scheme IS NOT NULL
          AND target_identity_value IS NOT NULL AND target_text IS NULL)
      OR (resolution!='resolved' AND target_identity_scheme IS NULL
          AND target_identity_value IS NULL AND target_text IS NOT NULL)),
  CHECK ((resolution='ambiguous' AND ambiguity=1)
      OR (resolution!='ambiguous' AND ambiguity=0)),
  CHECK ((relation='REFERENCES_SYMBOL' AND capability='references')
      OR (relation='CALLS' AND capability='calls')
      OR (relation='IMPORTS' AND capability='imports')
      OR (relation='HAS_TYPE' AND capability='types')
      OR (relation='TYPE_DEFINITION' AND capability='type_definitions')
      OR (relation='INHERITS' AND capability='inheritance')
      OR (relation='IMPLEMENTS' AND capability='implementations'))
) WITHOUT ROWID;

CREATE TABLE diagnostic (
  diagnostic_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  capability TEXT NOT NULL CHECK (capability='diagnostics'),
  severity TEXT NOT NULL CHECK (severity IN ('error','warning','information','hint')),
  code TEXT,
  message TEXT NOT NULL,
  byte_start INTEGER NOT NULL CHECK (byte_start >= 0),
  byte_end INTEGER NOT NULL CHECK (byte_end > byte_start),
  evidence_level TEXT NOT NULL CHECK (evidence_level IN ('compiler','semantic','syntax')),
  FOREIGN KEY (scope_id, run_id) REFERENCES analysis_scope(scope_id, run_id),
  FOREIGN KEY (scope_id, source_id) REFERENCES expected_source(scope_id, source_id),
  FOREIGN KEY (run_id, capability) REFERENCES run_capability(run_id, capability),
  UNIQUE (diagnostic_id, scope_id)
) WITHOUT ROWID;

CREATE TABLE diagnostic_related (
  diagnostic_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  scope_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  message TEXT,
  byte_start INTEGER NOT NULL CHECK (byte_start >= 0),
  byte_end INTEGER NOT NULL CHECK (byte_end > byte_start),
  PRIMARY KEY (diagnostic_id, ordinal),
  FOREIGN KEY (diagnostic_id, scope_id) REFERENCES diagnostic(diagnostic_id, scope_id),
  FOREIGN KEY (scope_id, source_id) REFERENCES expected_source(scope_id, source_id)
) WITHOUT ROWID;

CREATE TABLE slice_activation (
  slice_id TEXT PRIMARY KEY,
  slice_key TEXT NOT NULL,
  run_id TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  capability TEXT NOT NULL CHECK (capability IN
    ('definitions','declarations','references','calls','imports','types',
     'type_definitions','inheritance','implementations','diagnostics')),
  selected INTEGER NOT NULL CHECK (selected IN (0,1)),
  selection_reason TEXT NOT NULL CHECK (selection_reason IN
    ('new-complete','new-partial-terminal','retained-parent','complete-empty')),
  FOREIGN KEY (scope_id, run_id) REFERENCES analysis_scope(scope_id, run_id),
  FOREIGN KEY (run_id, capability) REFERENCES run_capability(run_id, capability),
  UNIQUE (slice_id, run_id, scope_id, capability)
) WITHOUT ROWID;

CREATE TABLE validity (
  validity_id TEXT PRIMARY KEY,
  symbol_claim_id TEXT REFERENCES symbol_claim(claim_id),
  relationship_claim_id TEXT REFERENCES relationship_claim(claim_id),
  diagnostic_id TEXT REFERENCES diagnostic(diagnostic_id),
  status TEXT NOT NULL CHECK (status IN ('current','soft-stale','hard-stale')),
  stale_reason TEXT,
  CHECK ((symbol_claim_id IS NOT NULL) + (relationship_claim_id IS NOT NULL)
       + (diagnostic_id IS NOT NULL) = 1),
  CHECK ((status='current' AND stale_reason IS NULL)
      OR (status!='current' AND stale_reason IS NOT NULL))
) WITHOUT ROWID;

CREATE INDEX analyzer_run_scope
ON analyzer_run(repository_id, checkout_id, analysis_sha256, run_id);
CREATE INDEX analyzer_run_publication
ON analyzer_run(publication_generation_id, outcome, run_id);
CREATE INDEX run_capability_reverse
ON run_capability(capability, run_id);
CREATE INDEX analysis_scope_run
ON analysis_scope(run_id, build_target, build_configuration, scope_id);
CREATE INDEX expected_source_reverse
ON expected_source(source_id, source_sha256, scope_id);
CREATE INDEX coverage_capability
ON coverage(scope_id, capability, status, source_id);
CREATE INDEX symbol_identity
ON symbol_claim(identity_scheme, identity_value, capability, claim_id);
CREATE INDEX symbol_source_span
ON symbol_claim(source_id, byte_start, byte_end, claim_id);
CREATE INDEX relationship_source
ON relationship_claim(source_identity_scheme, source_identity_value, capability, claim_id);
CREATE INDEX relationship_target
ON relationship_claim(target_identity_scheme, target_identity_value, capability, claim_id);
CREATE INDEX relationship_source_span
ON relationship_claim(source_id, byte_start, byte_end, claim_id);
CREATE INDEX diagnostic_source_span
ON diagnostic(source_id, byte_start, byte_end, severity, diagnostic_id);
CREATE INDEX diagnostic_related_source_span
ON diagnostic_related(source_id, byte_start, byte_end, diagnostic_id, ordinal);
CREATE UNIQUE INDEX one_selected_slice
ON slice_activation(slice_key) WHERE selected=1;
CREATE INDEX slice_run
ON slice_activation(run_id, scope_id, capability, selected, slice_id);
CREATE UNIQUE INDEX validity_symbol_once
ON validity(symbol_claim_id) WHERE symbol_claim_id IS NOT NULL;
CREATE UNIQUE INDEX validity_relationship_once
ON validity(relationship_claim_id) WHERE relationship_claim_id IS NOT NULL;
CREATE UNIQUE INDEX validity_diagnostic_once
ON validity(diagnostic_id) WHERE diagnostic_id IS NOT NULL;
CREATE INDEX validity_status
ON validity(status, validity_id);
```

Before insertion, require `expected_scope_count` and `expected_scope_set_sha256` to equal the canonical set of `(scope_id,source_manifest_sha256,target,configuration)` rows, and require every scope manifest digest to equal its parent run's `source_manifest_sha256`. For each scope, require `expected_source_count` and `expected_source_set_sha256` to equal the canonical `expected_source` membership; reject missing, duplicate, extra, or hash-mismatched membership and require exactly one coverage row for every expected-source/capability pair declared by the run. Re-run these checks from stored rows during database validation, so closed-world evaluation is reconstructible without an in-memory `NormalizedAnalysis`.

Use these exact canonical rows and fail-closed evaluator:

```python
def _set_sha256(rows: Sequence[Mapping[str, object]]) -> str:
    return hashlib.sha256(canonical_json_bytes(list(rows))).hexdigest()


def validate_persisted_scope(database: sqlite3.Connection, run_id: str) -> None:
    run = database.execute(
        "SELECT expected_scope_count,expected_scope_set_sha256,"
        "declared_capability_count,declared_capabilities_sha256 "
        "FROM analyzer_run WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if run is None:
        raise ValueError("analyzer run is missing")
    scopes = database.execute(
        "SELECT scope_id,source_manifest_sha256,build_target,build_configuration "
        "FROM analysis_scope "
        "WHERE run_id=? ORDER BY scope_id", (run_id,),
    ).fetchall()
    scope_rows = [
        {"scope_id": row[0], "source_manifest_sha256": row[1],
         "target": row[2], "configuration": row[3]}
        for row in scopes
    ]
    if len(scope_rows) != run[0] or _set_sha256(scope_rows) != run[1]:
        raise ValueError("persisted expected scope set is incomplete")
    capabilities = [
        {"capability": row[0]}
        for row in database.execute(
            "SELECT capability FROM run_capability WHERE run_id=? ORDER BY capability",
            (run_id,),
        ).fetchall()
    ]
    if len(capabilities) != run[2] or _set_sha256(capabilities) != run[3]:
        raise ValueError("persisted declared capability set is incomplete")
    for scope_id, _source_manifest_sha256, _target, _configuration in scopes:
        expected_count, expected_sha256 = database.execute(
            "SELECT expected_source_count,expected_source_set_sha256 "
            "FROM analysis_scope WHERE scope_id=?",
            (scope_id,),
        ).fetchone()
        sources = database.execute(
            "SELECT source_id,source_sha256,disposition FROM expected_source "
            "WHERE scope_id=? ORDER BY source_id", (scope_id,),
        ).fetchall()
        source_rows = [
            {"source_id": row[0], "sha256": row[1], "disposition": row[2]}
            for row in sources
        ]
        if len(source_rows) != expected_count or _set_sha256(source_rows) != expected_sha256:
            raise ValueError("persisted expected source set is incomplete")
        coverage_count = database.execute(
            "SELECT count(*) FROM coverage WHERE scope_id=?", (scope_id,),
        ).fetchone()[0]
        if coverage_count != expected_count * len(capabilities):
            raise ValueError("coverage does not span expected sources and capabilities")


def database_closed_world(
    database: sqlite3.Connection,
    scope_id: str,
    capability: Capability,
) -> bool:
    scope = database.execute(
        "SELECT expected_source_count,expected_source_set_sha256,generated_sources,"
        "dependency_resolution,analyzer_support FROM analysis_scope WHERE scope_id=?",
        (scope_id,),
    ).fetchone()
    if scope is None:
        return False
    sources = database.execute(
        "SELECT source_id,source_sha256,disposition FROM expected_source "
        "WHERE scope_id=? ORDER BY source_id", (scope_id,),
    ).fetchall()
    source_rows = [
        {"source_id": row[0], "sha256": row[1], "disposition": row[2]}
        for row in sources
    ]
    if len(source_rows) != scope[0] or _set_sha256(source_rows) != scope[1]:
        return False
    coverage = database.execute(
        "SELECT source_id,status,closed_world_eligible FROM coverage "
        "WHERE scope_id=? AND capability=? ORDER BY source_id",
        (scope_id, capability.value),
    ).fetchall()
    if [row[0] for row in coverage] != [row["source_id"] for row in source_rows]:
        return False
    return (
        scope[2] in {"available", "not-required"}
        and scope[3] == "complete"
        and scope[4] == "complete"
        and all(row[1] in {"complete", "excluded"} and row[2] == 1 for row in coverage)
    )
```

The polymorphic validity subject uses three nullable real foreign keys plus the exactly-one `CHECK`; the three partial unique indexes permit exactly one validity row per subject. `one_selected_slice` enforces at most one selected row per deterministic `slice_key`; validation requires exactly one selected row for every required slice key and rejects an unknown or missing key.

Extend `tests/code_kernel_helpers.py` with `basic_graph_records`, `snapshot_for_records`, `publish_v2_fixture`, `publish_v3_fixture`, `make_normalized_analysis_for_records`, `build_fixture_generation`, and `open_v3_fixture`. `publish_v3_fixture` calls `verify_native_analysis` and then the production verified writer; no test helper receives a DDL token or raw writer.

Also add the shared catalog fixture now that its production type exists:

```python
@pytest.fixture
def catalog(state_root: Path) -> GenerationCatalog:
    return GenerationCatalog(state_root)
```

Add this independent exact-signature test. The fixed digest below was calculated from the 30 ordered `sqlite_schema` rows produced by the DDL above; it is not imported from production. Any table, column, check, foreign key, partial predicate, index column, or SQL ordering change alters the digest.

```python
V3_EXTENSION_NAMES = frozenset({
    "analyzer_run", "run_capability", "analysis_scope", "expected_source", "coverage",
    "symbol_claim", "relationship_claim", "diagnostic", "diagnostic_related",
    "slice_activation", "validity", "analyzer_run_scope",
    "analyzer_run_publication", "run_capability_reverse", "analysis_scope_run", "expected_source_reverse",
    "coverage_capability", "symbol_identity", "symbol_source_span",
    "relationship_source", "relationship_target", "relationship_source_span",
    "diagnostic_source_span", "diagnostic_related_source_span",
    "one_selected_slice", "slice_run", "validity_symbol_once",
    "validity_relationship_once", "validity_diagnostic_once", "validity_status",
})
V3_EXTENSION_SCHEMA_SHA256 = "088316ec6aa481e52bdadb4534e8ab50bd7e76d8fbf2e6395da591214544efec"


def exact_extension_schema_sha256(database: sqlite3.Connection) -> str:
    bind_slots = ",".join("?" for _ in V3_EXTENSION_NAMES)
    rows = database.execute(
        f"SELECT type,name,tbl_name,sql FROM sqlite_schema "
        f"WHERE name IN ({bind_slots}) ORDER BY type,name",
        sorted(V3_EXTENSION_NAMES),
    ).fetchall()
    assert len(rows) == 30
    encoded = json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_v3_extension_has_exact_table_index_and_fk_signature(tmp_path: Path) -> None:
    path = build_verified_v3_fixture_for_schema_test(tmp_path)
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as database:
        assert exact_extension_schema_sha256(database) == V3_EXTENSION_SCHEMA_SHA256
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []


def build_verified_v3_fixture_for_schema_test(tmp_path: Path) -> Path:
    records = basic_graph_records()
    snapshot = snapshot_for_records(records)
    verified = verify_native_analysis(
        snapshot, make_normalized_analysis_for_records(records)
    )
    path = tmp_path / "verified-v3.sqlite3"
    create_generation_database(
        path,
        schema=GraphSchema.V3,
        verified_analyses=(verified,),
        **records,
    )
    return path


def build_damaged_v3_fixture(tmp_path: Path, damage: str) -> None:
    path = build_verified_v3_fixture_for_schema_test(tmp_path)
    with sqlite3.connect(path) as database:
        if damage == "missing-scope":
            database.execute(
                "DELETE FROM analysis_scope WHERE scope_id=(SELECT scope_id FROM analysis_scope LIMIT 1)"
            )
        elif damage == "extra-scope":
            database.execute(
                "INSERT INTO analysis_scope "
                "SELECT 'scope:extra',run_id,source_manifest_sha256,"
                "build_target||'-extra',build_configuration,0,?,"
                "generated_sources,dependency_resolution,analyzer_support "
                "FROM analysis_scope LIMIT 1",
                (hashlib.sha256(canonical_json_bytes([])).hexdigest(),),
            )
        elif damage == "duplicate-target-configuration":
            database.execute(
                "INSERT INTO analysis_scope "
                "SELECT 'scope:duplicate',run_id,source_manifest_sha256,"
                "build_target,build_configuration,0,?,"
                "generated_sources,dependency_resolution,analyzer_support "
                "FROM analysis_scope LIMIT 1",
                (hashlib.sha256(canonical_json_bytes([])).hexdigest(),),
            )
        elif damage == "scope-set-hash":
            database.execute("UPDATE analyzer_run SET expected_scope_set_sha256=?", ("f" * 64,))
        elif damage == "scope-source-manifest":
            database.execute(
                "UPDATE analysis_scope SET source_manifest_sha256=?", ("f" * 64,)
            )
        elif damage == "missing-capability":
            database.execute(
                "DELETE FROM run_capability WHERE (run_id,capability)="
                "(SELECT run_id,capability FROM run_capability LIMIT 1)"
            )
        elif damage == "extra-capability":
            run_id = database.execute("SELECT run_id FROM analyzer_run").fetchone()[0]
            database.execute("INSERT INTO run_capability VALUES (?, 'diagnostics')", (run_id,))
        elif damage == "capability-set-hash":
            database.execute("UPDATE analyzer_run SET declared_capabilities_sha256=?", ("f" * 64,))
        elif damage == "missing-source":
            database.execute(
                "DELETE FROM expected_source WHERE (scope_id,source_id)="
                "(SELECT scope_id,source_id FROM expected_source LIMIT 1)"
            )
        elif damage == "extra-source":
            empty_sha = hashlib.sha256(b"").hexdigest()
            database.execute(
                "INSERT INTO source(source_id,relative_path,sha256,size,media_type,language,git_oid,content) "
                "VALUES ('source:extra','pkg/extra.py',?,0,'text/x-python','python',NULL,X'')",
                (empty_sha,),
            )
            scope_id, run_id = database.execute(
                "SELECT scope_id,run_id FROM analysis_scope LIMIT 1"
            ).fetchone()
            database.execute(
                "INSERT INTO expected_source VALUES (?,?, 'source:extra',?,'included')",
                (scope_id, run_id, empty_sha),
            )
        elif damage == "source-set-hash":
            database.execute("UPDATE analysis_scope SET expected_source_set_sha256=?", ("f" * 64,))
        elif damage == "missing-coverage":
            database.execute(
                "DELETE FROM coverage WHERE (scope_id,source_id,capability)="
                "(SELECT scope_id,source_id,capability FROM coverage LIMIT 1)"
            )
        elif damage == "extra-coverage":
            database.execute("INSERT INTO coverage SELECT * FROM coverage LIMIT 1")
        elif damage == "duplicate-selected-slice":
            database.execute(
                "INSERT INTO slice_activation "
                "SELECT slice_id||':duplicate',slice_key,run_id,scope_id,capability,1,selection_reason "
                "FROM slice_activation WHERE selected=1 LIMIT 1"
            )
        elif damage == "missing-selected-slice":
            database.execute("UPDATE slice_activation SET selected=0 WHERE selected=1")
        elif damage == "invalid-validity-subject":
            database.execute(
                "INSERT INTO validity VALUES ('validity:invalid',NULL,NULL,NULL,'current',NULL)"
            )
        else:
            raise AssertionError(damage)
    validate_generation_database(path, schema=GraphSchema.V3)


@pytest.mark.parametrize("damage", [
    "missing-scope", "extra-scope", "duplicate-target-configuration",
    "scope-set-hash", "scope-source-manifest", "missing-capability",
    "extra-capability", "capability-set-hash",
    "missing-source", "extra-source", "source-set-hash",
    "missing-coverage", "extra-coverage", "duplicate-selected-slice",
    "missing-selected-slice", "invalid-validity-subject",
])
def test_v3_closed_world_storage_fails_closed(tmp_path: Path, damage: str) -> None:
    with pytest.raises((sqlite3.IntegrityError, ValueError)):
        build_damaged_v3_fixture(tmp_path, damage)
```

- [ ] **Step 4: Set and verify user_version transactionally**

Create tables and insert rows, execute `PRAGMA user_version=2|3`, commit, then call `validate_generation_database(path: Path, *, schema: GraphSchema) -> None`. It reopens read-only and requires the exact schema signature, expected `user_version`, `foreign_key_check`, stored membership/hash validation, selected-slice completeness, integrity check, and bounded row counts. Keep the existing exported `GRAPH_SCHEMA_VERSION` value at `evidence-graph/v2` for legacy consumers; never assign a global v3 constant that changes a v2 build. `_build_manifest` and `build_full_generation` add keyword-only `graph_schema: GraphSchema = GraphSchema.V2`; omission preserves every existing caller and emits matching v2 database/manifest identity. Precise production paths and every call carrying `verified_analyses` must pass `GraphSchema.V3` explicitly. Add a guard that rejects `verified_analyses` unless the selected schema is v3.

- [ ] **Step 5: Update manifest constraints and retain v2 reads**

The JSON schema accepts exactly `evidence-graph/v2` or `evidence-graph/v3`; both remain `corpus-generation/v2` and require the same source/graph/search artifacts. Catalog validation reads the manifest value, opens the matching exact validator, and rejects database/manifest mismatch.

- [ ] **Step 6: Run the complete schema/builder/catalog suites and verify GREEN**

Run: `uv run pytest tests/test_evidence_graph.py tests/test_evidence_graph_builder.py tests/test_evidence_graph_recovery.py tests/test_generation_catalog.py -q`

Expected: PASS. Existing omitted-schema callers remain v2 without mass call-site edits, explicit v2 remains v2, and only explicit v3 construction emits `user_version=3` and a v3 manifest. No module-level graph-schema stamp changes legacy output.

- [ ] **Step 7: Commit Task 4**

```bash
git add scripts/evidence_graph.py scripts/evidence_graph_builder.py scripts/generation_catalog.py scripts/schemas/evidence-graph-manifest-v1.json tests/code_kernel_helpers.py tests/test_evidence_graph.py tests/test_evidence_graph_builder.py tests/test_evidence_graph_recovery.py tests/test_generation_catalog.py
git commit -m "feat: add explicit evidence graph v3 schema"
```

### Task 5: Capture External Code And Seal Analyzer Workspaces

**Files:**
- Create: `scripts/code_workspace.py`
- Create: `tests/test_code_workspace.py`
- Modify: `scripts/corpus_snapshot.py:24-40,127-155,371-397,1417-end`
- Modify: `scripts/evidence_graph_builder.py:196-254,395-end`
- Modify: `scripts/generation_catalog.py:561-784`
- Modify: `scripts/schemas/evidence-graph-manifest-v1.json:5-104`
- Modify: `tests/test_corpus_snapshot.py`
- Modify: `tests/test_evidence_graph_builder.py`
- Modify: `tests/test_generation_catalog.py`
- Modify: `tests/test_doctor.py`
- Modify: `tests/code_kernel_helpers.py`

- [ ] **Step 1: Write failing capture and seal tests**

```python
def test_external_capture_is_normalized_bounded_and_utf8(repository: Path) -> None:
    snapshot = collect_repository_code(
        repository,
        roots=("pkg", "tests"),
        include_globs=("**/*.py",),
        ignore_globs=("**/__pycache__/**", "**/.venv/**"),
        suffixes=(".py",),
        limits=RepositoryCodeLimits(max_files=100, max_file_bytes=65536,
                                    max_total_bytes=1048576, max_depth=8),
    )
    assert all(path == unicodedata.normalize("NFC", path) for path, _ in snapshot.source_hashes)
    for source in snapshot.sources:
        source.content.decode("utf-8", errors="strict")


def test_sealed_workspace_is_snapshot_bound_and_detects_mutation(repository: Path, tmp_path: Path) -> None:
    snapshot = capture(repository)
    workspace = seal_workspace(snapshot, tmp_path / "run/input/workspace")
    assert verify_workspace_seal(workspace, snapshot) is True
    (workspace.root / "pkg/api.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(WorkspaceChanged):
        verify_workspace_seal(workspace, snapshot)


def capture(repository: Path) -> CorpusSnapshot:
    return collect_repository_code(
        repository,
        roots=("pkg", "tests"),
        include_globs=("**/*.py",),
        ignore_globs=("**/__pycache__/**", "**/.venv/**"),
        suffixes=(".py",),
        limits=RepositoryCodeLimits(),
    )


@pytest.fixture
def snapshot(repository: Path) -> CorpusSnapshot:
    return capture(repository)


@pytest.fixture
def scope(repository: Path) -> RepositoryScope:
    return resolve_repository_scope(repository)


def test_new_generation_manifest_persists_closed_capture_contract(
    repository: Path, catalog: GenerationCatalog, scope: RepositoryScope,
) -> None:
    result = publish_v2_fixture(catalog, capture(repository), scope, activate=False)
    capture_contract = result.manifest["code_capture"]
    assert set(capture_contract) == {
        "policy", "limits", "files", "directories", "membership_sha256"
    }
    assert capture_contract["policy"]["roots"] == ["pkg", "tests"]
    assert capture_contract["limits"]["max_files"] == 10_000
    assert all(set(item) == {"source_id", "relative_path", "sha256", "stat"}
               for item in capture_contract["files"])
    validate_generation_manifest(result.manifest)


def test_capture_contract_rejects_unknown_properties(
    repository: Path, catalog: GenerationCatalog, scope: RepositoryScope,
) -> None:
    result = publish_v2_fixture(catalog, capture(repository), scope, activate=False)
    damaged = copy.deepcopy(result.manifest)
    damaged["code_capture"]["unexpected"] = True
    with pytest.raises(ValueError, match="additional|unexpected"):
        validate_generation_manifest(damaged)


def test_non_code_corpus_build_omits_code_capture_and_remains_valid(
    catalog: GenerationCatalog, repository: Path,
) -> None:
    result = build_full_generation(
        catalog, generation_id="non-code", activate=False,
        repository_scope=resolve_repository_scope(repository),
        **basic_graph_records(),
    )
    assert result.manifest["graph_schema_version"] == "evidence-graph/v2"
    assert "code_capture" not in result.manifest
    validate_generation_manifest(result.manifest)


def test_v3_generation_publication_requires_capture_contract(
    catalog: GenerationCatalog,
) -> None:
    records = basic_graph_records()
    snapshot = snapshot_for_records(records)
    verified = verify_native_analysis(
        snapshot, make_normalized_analysis_for_records(records)
    )
    with pytest.raises(ValueError, match="code_capture"):
        build_full_generation(
            catalog, generation_id="invalid-code", graph_schema=GraphSchema.V3,
            verified_analyses=(verified,), **records,
        )


def test_low_level_v3_writer_has_no_duplicate_code_capture_argument(tmp_path: Path) -> None:
    records = basic_graph_records()
    snapshot = snapshot_for_records(records)
    verified = verify_native_analysis(
        snapshot, make_normalized_analysis_for_records(records)
    )
    assert "code_capture" not in inspect.signature(create_generation_database).parameters
    selected = create_generation_database(
        tmp_path / "low-level-v3.sqlite3", schema=GraphSchema.V3,
        verified_analyses=(verified,), **records,
    )
    assert selected == GraphSchema.V3


def test_doctor_accepts_existing_non_code_v2_without_code_capture(
    state_root: Path, non_code_v2_generation: BuildResult,
) -> None:
    report = run_doctor(root=state_root, state_root=state_root)
    generation = next(
        item for item in report["generations"]
        if item["generation_id"] == non_code_v2_generation.generation_id
    )
    assert generation["status"] == "ok"
    assert "code_capture" not in non_code_v2_generation.manifest
```

Add `non_code_v2_generation` to registered `tests.code_kernel_helpers`; it uses `build_full_generation(..., graph_schema=GraphSchema.V2, code_capture=None)` with ordinary corpus records and activates it through the existing catalog API. It must not call `collect_repository_code`.

Add platform tests for symlink escape, Windows reparse point, casefold collision, NFC collision, absolute/parent roots, path traversal, invalid UTF-8, file growth during chunked capture, too many entries, depth, suffix filtering, and changed live source during initial capture.

- [ ] **Step 2: Run workspace tests and verify RED**

Run: `uv run pytest tests/test_code_workspace.py tests/test_corpus_snapshot.py -q -k "external_capture or sealed_workspace"`

Expected: FAIL because `RepositoryCodeLimits` and sealed workspaces do not exist.

- [ ] **Step 3: Implement exact capture limits and policy**

```python
@dataclass(frozen=True, slots=True)
class RepositoryCodeLimits:
    max_files: int = 10_000
    max_file_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024
    max_entries: int = 50_000
    max_directories: int = 5_000
    max_depth: int = 32
    chunk_bytes: int = 64 * 1024


@dataclass(frozen=True, slots=True)
class RepositoryCodePolicy:
    roots: tuple[str, ...]
    include_globs: tuple[str, ...]
    ignore_globs: tuple[str, ...]
    suffixes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FileStatMetadata:
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class DirectoryMembership:
    relative_path: str
    entry_count: int
    entries_sha256: str


@dataclass(frozen=True, slots=True)
class CodeCaptureContract:
    policy: RepositoryCodePolicy
    limits: RepositoryCodeLimits
    files: tuple[tuple[str, str, str, FileStatMetadata], ...]
    directories: tuple[DirectoryMembership, ...]
    membership_sha256: str


def collect_repository_code(
    checkout_root: Path,
    *,
    roots: tuple[str, ...],
    include_globs: tuple[str, ...],
    ignore_globs: tuple[str, ...],
    suffixes: tuple[str, ...],
    limits: RepositoryCodeLimits,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> CorpusSnapshot:
    """Capture one bounded normalized repository-code snapshot."""
```

Add `code_capture: CodeCaptureContract | None = None` to `CorpusSnapshot`; `collect_repository_code` always sets it, while non-code legacy collectors leave it `None`. Resolve the repository scope first. Roots and glob matches are normalized relative POSIX NFC paths. Reject empty/dot/parent/absolute paths, backslashes, links, reparse points, device files, casefold or NFC collisions, and any resolved path outside `checkout_root`. Always ignore `.git`, runtime dirs, virtual environments, and bytecode caches. Read in `chunk_bytes` chunks with descriptor identity/size/mtime verification before and after. Decode `.py` strictly as UTF-8 before admitting it.

- [ ] **Step 4: Persist the exact closed capture contract only for code generations**

Add keyword-only `code_capture: CodeCaptureContract | None = None` to `_build_manifest` and `build_full_generation`, not to `create_generation_database`. Preserve omission for ordinary `collect_corpus`/memory builds and legacy v2 callers. Require and emit it at generation publication when `graph_schema is GraphSchema.V3`, `verified_analyses` is non-empty, or the explicit code-analysis builder path is selected. `build_full_generation` validates this boundary before constructing/registering the manifest, then passes only verified batches and source rows/bytes to the low-level database writer. Legacy registered v2 generations without it remain structurally readable, but Task 12 labels code results from them `freshness=unknown` and never `current`. `generation_catalog._validate_generation` validates the closed object when present and recomputes `membership_sha256` from sorted files/directories. Add this optional top-level property to `scripts/schemas/evidence-graph-manifest-v1.json`; because the root already uses `additionalProperties: false`, no other schema relaxation is allowed and the root `required` list does not gain `code_capture`:

```json
"code_capture": {
  "type": "object",
  "required": ["policy", "limits", "files", "directories", "membership_sha256"],
  "properties": {
    "policy": {
      "type": "object",
      "required": ["roots", "include_globs", "ignore_globs", "suffixes"],
      "properties": {
        "roots": {"type": "array", "minItems": 1, "maxItems": 128, "uniqueItems": true, "items": {"type": "string", "minLength": 1, "maxLength": 4096}},
        "include_globs": {"type": "array", "minItems": 1, "maxItems": 256, "uniqueItems": true, "items": {"type": "string", "minLength": 1, "maxLength": 4096}},
        "ignore_globs": {"type": "array", "maxItems": 256, "uniqueItems": true, "items": {"type": "string", "minLength": 1, "maxLength": 4096}},
        "suffixes": {"type": "array", "minItems": 1, "maxItems": 128, "uniqueItems": true, "items": {"type": "string", "minLength": 1, "maxLength": 128}}
      },
      "additionalProperties": false
    },
    "limits": {
      "type": "object",
      "required": ["max_files", "max_file_bytes", "max_total_bytes", "max_entries", "max_directories", "max_depth", "chunk_bytes"],
      "properties": {
        "max_files": {"type": "integer", "minimum": 1, "maximum": 1000000},
        "max_file_bytes": {"type": "integer", "minimum": 1, "maximum": 1073741824},
        "max_total_bytes": {"type": "integer", "minimum": 1, "maximum": 17179869184},
        "max_entries": {"type": "integer", "minimum": 1, "maximum": 5000000},
        "max_directories": {"type": "integer", "minimum": 1, "maximum": 1000000},
        "max_depth": {"type": "integer", "minimum": 1, "maximum": 256},
        "chunk_bytes": {"type": "integer", "minimum": 4096, "maximum": 8388608}
      },
      "additionalProperties": false
    },
    "files": {
      "type": "array", "maxItems": 1000000,
      "items": {
        "type": "object",
        "required": ["source_id", "relative_path", "sha256", "stat"],
        "properties": {
          "source_id": {"type": "string", "minLength": 1, "maxLength": 512},
          "relative_path": {"type": "string", "minLength": 1, "maxLength": 4096},
          "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
          "stat": {
            "type": "object",
            "required": ["size", "mtime_ns", "ctime_ns", "mode", "device", "inode"],
            "properties": {
              "size": {"type": "integer", "minimum": 0},
              "mtime_ns": {"type": "integer", "minimum": 0},
              "ctime_ns": {"type": "integer", "minimum": 0},
              "mode": {"type": "integer", "minimum": 0},
              "device": {"type": "integer", "minimum": 0},
              "inode": {"type": "integer", "minimum": 0}
            },
            "additionalProperties": false
          }
        },
        "additionalProperties": false
      }
    },
    "directories": {
      "type": "array", "maxItems": 1000000,
      "items": {
        "type": "object",
        "required": ["relative_path", "entry_count", "entries_sha256"],
        "properties": {
          "relative_path": {"type": "string", "maxLength": 4096},
          "entry_count": {"type": "integer", "minimum": 0},
          "entries_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}
        },
        "additionalProperties": false
      }
    },
    "membership_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}
  },
  "additionalProperties": false
}
```

Add one root `allOf` branch: `if graph_schema_version` is `evidence-graph/v3`, `then required: ["code_capture"]`. The v2 branch does not require it. `build_full_generation` additionally rejects non-empty `verified_analyses` without `code_capture`, before manifest construction; direct low-level database creation remains manifest-agnostic.

Capture each file's `fstat` metadata from the same no-follow descriptor used for hashing. For every traversed directory, hash the sorted canonical `(NFC name, kind)` entries, including ignored entries, so additions/deletions select that directory for policy-filtered reconciliation. Metadata is a change detector only; it never substitutes for selective content hashing.

- [ ] **Step 5: Implement immutable sealed workspace materialization**

```python
@dataclass(frozen=True, slots=True)
class SealedWorkspace:
    root: Path
    source_manifest_sha256: str
    entries: tuple[tuple[str, str, int], ...]
    owner_only: bool
    read_only_requested: bool


def seal_workspace(snapshot: CorpusSnapshot, root: Path) -> SealedWorkspace:
    """Materialize captured bytes into one verified analyzer input tree."""
```

Create every directory and file with exclusive no-follow operations, write only captured bytes, fsync files/directories, set files read-only where supported, and persist no writable source path. Permission bits are defense in depth, not a sandbox claim. `verify_workspace_seal` reopens each member no-follow, rehashes chunkwise, rejects extra/missing entries, and compares the canonical source manifest.

- [ ] **Step 6: Run capture/seal/manifest tests across current platform and verify GREEN**

Run: `uv run pytest tests/test_code_workspace.py tests/test_corpus_snapshot.py tests/test_evidence_graph_builder.py tests/test_generation_catalog.py tests/test_doctor.py -q`

Expected: PASS; unsupported reparse tests skip only when the host cannot create the test primitive. Existing non-code `collect_corpus` and doctor generation validation remain green with no `code_capture` property.

- [ ] **Step 7: Commit Task 5**

```bash
git add scripts/code_workspace.py scripts/corpus_snapshot.py scripts/evidence_graph_builder.py scripts/generation_catalog.py scripts/schemas/evidence-graph-manifest-v1.json tests/code_kernel_helpers.py tests/test_code_workspace.py tests/test_corpus_snapshot.py tests/test_evidence_graph_builder.py tests/test_generation_catalog.py tests/test_doctor.py
git commit -m "feat: seal captured analyzer workspaces"
```

### Task 6: Add Exact Invocation Consent And Linearized Start Leases

**Files:**
- Create: `scripts/code_consent.py`
- Create: `tests/test_code_consent.py`
- Modify: `scripts/repository_scope.py:150-204`

- [ ] **Step 1: Write failing consent and revoke/start race tests**

```python
def test_consent_binds_repository_analyzer_and_exact_invocation(state_root: Path, repository: Path) -> None:
    store = ConsentStore(state_root)
    request = consent_request(repository, invocation=("scip-python", "index", "<WORKSPACE>"))
    grant = store.grant(request, accepted_weaker_boundary=False)
    assert store.acquire_start(request, run_id="run:" + "1" * 64).grant_id == grant.grant_id
    changed = replace(request, invocation_sha256="f" * 64)
    with pytest.raises(PermissionError):
        store.acquire_start(changed, run_id="run:" + "2" * 64)


def test_revoke_and_start_are_serialized(state_root: Path, repository: Path) -> None:
    request = consent_request(repository)
    with ConsentStore(state_root) as setup:
        setup.grant(request, accepted_weaker_boundary=True)

    def separate_connection_action(action: str) -> str:
        with ConsentStore(state_root) as store:
            return race_action(store, request, action)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(separate_connection_action, ("start", "revoke")))
    assert results in (["started", "revoked-and-cancel-requested"],
                       ["denied", "revoked"])


def race_action(store: ConsentStore, request: ConsentRequest, action: str) -> str:
    if action == "start":
        try:
            store.acquire_start(request, run_id="run:" + "a" * 64)
        except PermissionError:
            return "denied"
        return "started"
    result = store.revoke(request)
    return "revoked-and-cancel-requested" if result.cancelled_jobs else "revoked"
```

- [ ] **Step 2: Run consent tests and verify RED**

Run: `uv run pytest tests/test_code_consent.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'code_consent'`.

- [ ] **Step 3: Implement the operational database using current helper signatures**

```python
class ConsentStore:
    def __init__(
        self,
        state_root: Path,
        *,
        clock: Callable[[], datetime] = utc_now,
        owner: ProcessOwner | None = None,
        read_only: bool = False,
    ) -> None:
        validate_state_root(state_root)
        self.state_root = Path(state_root).resolve(strict=True)
        self.path = self.state_root / "run/code-analysis-consent.sqlite3"
        self.owner = current_process_owner() if owner is None else owner
        if read_only:
            self.database = open_readonly_operational_db(
                self.path, self.state_root, max_bytes=64 * 1024 * 1024,
                owner_only=True,
            )
            validate_consent_schema(self.database)
            return
        self.database = open_operational_db(self.path, busy_ms=5_000)
        self.database.execute("BEGIN IMMEDIATE")
        try:
            for statement in CONSENT_SCHEMA_STATEMENTS:
                self.database.execute(statement)
            self.database.execute("PRAGMA user_version=1")
            self.database.commit()
        except BaseException:
            self.database.rollback()
            raise
        if self.database.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("consent schema user_version is not 1")
```

`CONSENT_SCHEMA_STATEMENTS` contains these four individual statements, not an `executescript()` string; Python's `executescript()` may commit a pending transaction:

```sql
CREATE TABLE IF NOT EXISTS consent_grant (
  grant_id TEXT PRIMARY KEY,
  repository_id TEXT NOT NULL,
  analyzer_family TEXT NOT NULL,
  invocation_sha256 TEXT NOT NULL,
  isolation_profile_sha256 TEXT NOT NULL,
  network_requested INTEGER NOT NULL CHECK (network_requested IN (0,1)),
  accepted_weaker_boundary INTEGER NOT NULL CHECK (accepted_weaker_boundary IN (0,1)),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  granted_at TEXT NOT NULL,
  revoked_at TEXT,
  UNIQUE (repository_id, analyzer_family, invocation_sha256,
          isolation_profile_sha256, network_requested, revision),
  UNIQUE (grant_id, revision),
  CHECK (revoked_at IS NULL OR revoked_at >= granted_at)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS analyzer_job (
  run_id TEXT PRIMARY KEY,
  filesystem_run_id TEXT NOT NULL UNIQUE,
  analysis_mode TEXT NOT NULL CHECK (analysis_mode IN ('precise','native-syntax')),
  lease_id TEXT UNIQUE,
  grant_id TEXT,
  grant_revision INTEGER CHECK (grant_revision IS NULL OR grant_revision >= 1),
  repository_id TEXT NOT NULL,
  checkout_id TEXT NOT NULL,
  source_manifest_sha256 TEXT NOT NULL,
  analysis_sha256 TEXT,
  invocation_sha256 TEXT NOT NULL,
  owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
  owner_start TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  lease_expires_at TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN
    ('starting','running','analyzed','ready_for_publication','publishing',
     'completed','failed','cancelled','rejected','quarantined')),
  cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
  publication_generation_id TEXT,
  publication_expected_active TEXT,
  ready_at TEXT,
  publication_started_at TEXT,
  receipt_path TEXT,
  receipt_sha256 TEXT,
  retain_until TEXT,
  started_at TEXT NOT NULL,
  terminal_at TEXT,
  terminal_reason TEXT,
  CHECK ((state IN ('starting','running') AND receipt_sha256 IS NULL
          AND analysis_sha256 IS NULL
          AND publication_generation_id IS NULL AND ready_at IS NULL
          AND publication_started_at IS NULL)
      OR (state='analyzed'
          AND analysis_sha256 IS NOT NULL
          AND ((analysis_mode='precise' AND receipt_sha256 IS NOT NULL)
            OR (analysis_mode='native-syntax' AND receipt_sha256 IS NULL))
          AND publication_generation_id IS NULL AND ready_at IS NULL
          AND publication_started_at IS NULL)
      OR (state='ready_for_publication'
          AND analysis_sha256 IS NOT NULL
          AND ((analysis_mode='precise' AND receipt_sha256 IS NOT NULL)
            OR (analysis_mode='native-syntax' AND receipt_sha256 IS NULL))
          AND publication_generation_id IS NOT NULL AND ready_at IS NOT NULL
          AND publication_started_at IS NULL)
      OR (state IN ('publishing','completed')
          AND analysis_sha256 IS NOT NULL
          AND ((analysis_mode='precise' AND receipt_sha256 IS NOT NULL)
            OR (analysis_mode='native-syntax' AND receipt_sha256 IS NULL))
          AND publication_generation_id IS NOT NULL AND ready_at IS NOT NULL
          AND publication_started_at IS NOT NULL)
      OR (state IN ('failed','cancelled','rejected','quarantined'))),
  CHECK ((state IN ('completed','failed','cancelled','rejected','quarantined')
          AND terminal_at IS NOT NULL)
      OR (state NOT IN ('completed','failed','cancelled','rejected','quarantined')
          AND terminal_at IS NULL)),
  CHECK ((state='completed' AND terminal_reason IS NULL)
      OR (state IN ('failed','cancelled','rejected','quarantined')
          AND terminal_reason IS NOT NULL)
      OR (state NOT IN ('completed','failed','cancelled','rejected','quarantined')
          AND terminal_reason IS NULL)),
  CHECK ((receipt_sha256 IS NULL) = (receipt_path IS NULL)),
  CHECK ((analysis_mode='precise' AND lease_id IS NOT NULL AND grant_id IS NOT NULL
          AND grant_revision IS NOT NULL)
      OR (analysis_mode='native-syntax' AND lease_id IS NULL AND grant_id IS NULL
          AND grant_revision IS NULL AND receipt_sha256 IS NULL)),
  CHECK (lease_expires_at > heartbeat_at),
  CHECK (state!='completed' OR (analysis_sha256 IS NOT NULL
        AND publication_generation_id IS NOT NULL
        AND publication_started_at IS NOT NULL
        AND ((analysis_mode='precise' AND receipt_sha256 IS NOT NULL)
          OR analysis_mode='native-syntax'))),
  FOREIGN KEY (grant_id, grant_revision) REFERENCES consent_grant(grant_id, revision)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS consent_active_match
ON consent_grant(repository_id, analyzer_family, invocation_sha256,
                 isolation_profile_sha256, network_requested, revoked_at, revision);

CREATE INDEX IF NOT EXISTS analyzer_job_live
ON analyzer_job(repository_id, state, cancel_requested, run_id);
```

Because each string is executed separately, `CONSENT_SCHEMA_STATEMENTS` has four entries in the exact order above. `ConsentStore.close`, `__enter__`, and `__exit__` close each connection; a store is never shared across threads.

Define frozen `ProcessOwner(owner_pid: int, owner_start: str)` before `ConsentStore`. `owner_start` is `process-start/v1:` plus `secrets.token_hex(32)`, generated once at module import and cached with `os.getpid()`; it is a process-instance fencing nonce, not an OS timestamp. `current_process_owner()` recreates it after a fork/PID change. PID liveness is advisory only: an owner is live only while `now < lease_expires_at`, the PID exists, and every heartbeat/transition presents the exact `(owner_pid, owner_start)` pair. This bounds PID-reuse ambiguity by the 30-second lease and makes the stored owner diagnosable on every platform.

Define one `is_process_alive(pid: int) -> bool` in `code_consent.py` and reuse it from doctor recovery. It rejects `pid <= 0`; on Windows it uses `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` plus `GetExitCodeProcess`, treating access denied as indeterminate/live rather than reclaimable; on POSIX it uses `os.kill(pid, 0)`, treating `ProcessLookupError` as dead and `PermissionError` as live. Tests cover PID zero, dead, access-denied, and live branches through injected platform adapters. Liveness alone never extends a lease.

`acquire_start` uses `BEGIN IMMEDIATE`, selects an active matching grant, inserts precise `starting`, commits, and returns immutable `ConsentLease(grant_id, grant_revision, lease_id, run_id, repository_id, checkout_id, source_manifest_sha256, invocation_sha256, owner)`. Every precise lease check compares all identity fields. `start_native_job(...) -> NativeJob` inserts `analysis_mode='native-syntax'` with null consent/lease/receipt fields and the same process owner; this is lease-free with respect to consent and precise execution, while still using the operational owner-expiry fence needed for recovery. Both modes start with `analysis_sha256=NULL`. The runner's verified transition and `mark_native_analyzed` atomically populate the exact `analysis_sha256` while moving `running -> analyzed`; neither mode may reach `analyzed` without it. `mark_ready_for_publication` rechecks the stored `source_manifest_sha256` and `analysis_sha256` against the verified batch and Graph run before recording generation/expected-active identity. The precise final gate transitions `ready_for_publication -> publishing` inside its uncommitted consent transaction; native publication performs the corresponding owner-fenced state transition without a consent lookup. Only a successful catalog commit permits `publishing -> completed`. `revoke` uses a different connection, marks the grant revoked, and sets `cancel_requested=1` only on matching precise jobs. SQLite's one-writer rule linearizes these transitions and revocation.

Add `heartbeat_job(run_id, owner, *, now) -> JobHeartbeat`, using `BEGIN IMMEDIATE` and an exact nonterminal-state/owner CAS to set `heartbeat_at=now` and `lease_expires_at=now+30s`. A wrong owner, terminal row, expired lease, or zero-row update raises `LeaseLost`; heartbeats never resurrect a row. `AnalyzerHeartbeat` is a non-daemon context-managed thread that waits 10 seconds on a `threading.Event`, opens and closes its own `ConsentStore` for each refresh, records the first failure, and on exit signals, joins, and requires no lost heartbeat. The orchestrator also refreshes synchronously at capture, pre-spawn/native-analysis, post-analysis, pre-build, and pre-publication boundaries.

- [ ] **Step 4: Test repository-wide consent and checkout-specific jobs**

Linked worktrees share repository consent but leases bind the exact checkout ID and snapshot. Use this exact mutation test; `verify_lease` must reject each case before spawn or publication:

```python
@pytest.mark.parametrize("field,value", [
    ("checkout_id", "checkout:" + "b" * 64),
    ("source_manifest_sha256", "b" * 64),
    ("run_id", "run:" + "b" * 64),
    ("lease_id", "lease:" + "b" * 64),
    ("grant_revision", 999),
    ("invocation_sha256", "b" * 64),
])
def test_lease_is_not_reusable(
    state_root: Path, repository: Path, field: str, value: object,
) -> None:
    request = consent_request(repository)
    with ConsentStore(state_root) as store:
        store.grant(request, accepted_weaker_boundary=True)
        lease = store.acquire_start(request, run_id="run:" + "a" * 64)
        with pytest.raises(PermissionError):
            store.verify_lease(replace(lease, **{field: value}))


def test_completed_state_requires_publication_identity(state_root: Path, repository: Path) -> None:
    request = consent_request(repository)
    with ConsentStore(state_root) as store:
        store.grant(request, accepted_weaker_boundary=True)
        lease = store.acquire_start(request, run_id="run:" + "c" * 64)
        with pytest.raises(sqlite3.IntegrityError):
            store.database.execute(
                "UPDATE analyzer_job SET state='completed',terminal_at=? WHERE run_id=?",
                ("2026-07-21T00:00:00+00:00", lease.run_id),
            )


def test_native_job_has_owner_but_no_consent_lease_or_receipt(
    state_root: Path, repository: Path,
) -> None:
    owner = ProcessOwner(4312, "process-start/v1:" + "a" * 64)
    with ConsentStore(state_root, owner=owner) as store:
        job = store.start_native_job(native_job_request(repository), run_id="run:" + "e" * 64)
        row = store.job(job.run_id)
    assert row.analysis_mode == "native-syntax"
    assert (row.owner_pid, row.owner_start) == (owner.owner_pid, owner.owner_start)
    assert row.lease_id is None and row.grant_id is None and row.receipt_sha256 is None


@dataclass
class MutableClock:
    value: datetime = datetime(2026, 7, 21, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


def test_heartbeat_requires_exact_process_instance_and_extends_expiry(
    state_root: Path, repository: Path, clock: MutableClock,
) -> None:
    owner = ProcessOwner(4312, "process-start/v1:" + "a" * 64)
    with ConsentStore(state_root, owner=owner, clock=clock) as store:
        job = store.start_native_job(native_job_request(repository), run_id="run:" + "d" * 64)
        before = store.job(job.run_id).lease_expires_at
        clock.advance(seconds=10)
        after = store.heartbeat_job(job.run_id, owner, now=clock()).lease_expires_at
        assert after > before
        with pytest.raises(LeaseLost):
            store.heartbeat_job(
                job.run_id,
                replace(owner, owner_start="process-start/v1:" + "b" * 64),
                now=clock(),
            )
        clock.advance(seconds=31)
        with pytest.raises(LeaseLost):
            store.heartbeat_job(job.run_id, owner, now=clock())


def test_analysis_sha256_is_null_before_analysis_and_required_afterward(
    state_root: Path, repository: Path,
) -> None:
    owner = ProcessOwner(4312, "process-start/v1:" + "c" * 64)
    with ConsentStore(state_root, owner=owner) as store:
        job = store.start_native_job(
            native_job_request(repository), run_id="run:" + "c" * 64
        )
        store.mark_native_running(job, owner)
        assert store.job(job.run_id).analysis_sha256 is None
        with pytest.raises(sqlite3.IntegrityError):
            store.database.execute(
                "UPDATE analyzer_job SET state='analyzed' WHERE run_id=?", (job.run_id,)
            )
        store.mark_native_analyzed(job, owner, analysis_sha256="a" * 64)
        assert store.job(job.run_id).analysis_sha256 == "a" * 64
        with pytest.raises(sqlite3.IntegrityError):
            store.database.execute(
                "UPDATE analyzer_job SET state='ready_for_publication',"
                "analysis_sha256=NULL,publication_generation_id='gen',ready_at=? "
                "WHERE run_id=?",
                ("2026-07-21T00:00:01+00:00", job.run_id),
            )
```

Every concurrency test constructs and closes one `ConsentStore` per worker, proving separate SQLite connections and retaining `open_operational_db(path, busy_ms=5_000)` plus `validate_state_root(state_root) -> None` semantics.

- [ ] **Step 5: Run consent/repository tests and verify GREEN**

Run: `uv run pytest tests/test_code_consent.py tests/test_repository_scope.py -q`

Expected: PASS, with rollback journal, `synchronous=FULL`, `user_version=1`, and no WAL.

- [ ] **Step 6: Commit Task 6**

```bash
git add scripts/code_consent.py scripts/repository_scope.py tests/test_code_consent.py
git commit -m "feat: linearize analyzer consent and starts"
```

### Task 7: Run Analyzers Against Sealed Inputs With Qualified Controls

**Files:**
- Create: `scripts/code_runner.py`
- Create: `scripts/code_posix_launcher.py`
- Create: `tests/test_code_runner.py`
- Create: `tests/test_code_posix_launcher.py`
- Modify: `scripts/secret_redact.py`
- Modify: `scripts/code_consent.py`

- [ ] **Step 1: Write failing security, cancellation, and race tests**

```python
@pytest.fixture
def job_fixture(state_root: Path, repository: Path) -> RunnerFixture:
    return RunnerFixture.create(state_root=state_root, repository=repository)


@pytest.fixture
def fake_win32() -> FakeWin32Harness:
    return FakeWin32Harness.create()


def test_runner_receives_sealed_workspace_not_live_checkout(job_fixture: RunnerFixture) -> None:
    result = run_analyzer(job_fixture.job, lease=job_fixture.lease,
                          consent_store=job_fixture.store, limits=AnalyzerLimits())
    assert result.receipt.source_manifest_sha256 == job_fixture.workspace.source_manifest_sha256
    assert str(job_fixture.repository) not in result.receipt.sanitized_command
    assert result.receipt.workspace_root == "<SEALED_WORKSPACE>"
    assert job_fixture.store.job_state(job_fixture.lease.run_id) == "analyzed"
    row = job_fixture.store.job(job_fixture.lease.run_id)
    assert row.source_manifest_sha256 == result.receipt.source_manifest_sha256
    assert row.analysis_sha256 == result.receipt.analysis_sha256


def test_input_mutation_rejects_output(job_fixture: RunnerFixture) -> None:
    with pytest.raises(WorkspaceChanged):
        run_analyzer(job_fixture.mutating_job, lease=job_fixture.lease,
                     consent_store=job_fixture.store, limits=AnalyzerLimits())
    assert job_fixture.store.job_state(job_fixture.lease.run_id) == "rejected"


def test_output_growth_and_cancellation_kill_the_tree(job_fixture: RunnerFixture) -> None:
    with pytest.raises(ValueError, match="output"):
        run_analyzer(job_fixture.growing_output_job, lease=job_fixture.lease,
                     consent_store=job_fixture.store,
                     limits=AnalyzerLimits(max_output_bytes=1024))
    assert job_fixture.child_processes_alive() == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows process creation contract")
def test_windows_assigns_configured_job_before_entrypoint_can_run(fake_win32) -> None:
    process = spawn_windows_suspended(fake_win32.command, fake_win32.limits,
                                      api=fake_win32.api)
    assert fake_win32.api.calls == [
        "CreateJobObjectW", "SetInformationJobObject",
        "CreateProcessW(CREATE_SUSPENDED)", "AssignProcessToJobObject",
        "ResumeThread",
    ]
    assert process.entrypoint_observed_before_resume is False


@pytest.mark.skipif(os.name != "nt", reason="Windows process creation contract")
@pytest.mark.parametrize("failure", ["SetInformationJobObject", "AssignProcessToJobObject"])
def test_windows_job_failure_terminates_suspended_process_without_resume(fake_win32, failure) -> None:
    fake_win32.api.fail_at = failure
    with pytest.raises(OSError):
        spawn_windows_suspended(fake_win32.command, fake_win32.limits,
                                api=fake_win32.api)
    assert "ResumeThread" not in fake_win32.api.calls
    assert fake_win32.entrypoint_count == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher contract")
def test_posix_launch_uses_trusted_launcher_without_preexec_fn() -> None:
    launch = prepare_posix_launch(
        executable=Path("/bin/true"),
        arguments=(),
        limits=AnalyzerLimits(memory_bytes=64 * 1024 * 1024, child_count=2),
    )
    assert launch.command[:3] == (sys.executable, "-I", str(POSIX_LAUNCHER))
    assert launch.popen_kwargs["preexec_fn"] is None
    assert "RLIMIT_NPROC" not in json.loads(launch.command[3])
    assert launch.isolation.child_count == "trusted-execution-required"


class ExecObserved(Exception):
    pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher contract")
def test_posix_launcher_applies_available_limits_then_execs(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(resource, "setrlimit", lambda key, value: calls.append((key, value)))
    monkeypatch.setattr(os, "setsid", lambda: calls.append(("setsid", None)))
    monkeypatch.setattr(os, "write", lambda *_args: 1)
    monkeypatch.setattr(os, "execve", lambda *_args: (_ for _ in ()).throw(ExecObserved()))
    with pytest.raises(ExecObserved):
        code_posix_launcher.main([
            "launcher", json.dumps({"RLIMIT_FSIZE": 4096}), "/bin/true",
        ])
    assert calls[-1] == ("setsid", None)
```

- [ ] **Step 2: Run runner tests and verify RED**

Run: `uv run pytest tests/test_code_runner.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'code_runner'`.

- [ ] **Step 3: Define per-control qualification instead of one sandbox boolean**

```python
@dataclass(frozen=True, slots=True)
class IsolationReport:
    process_tree: Literal["enforced", "trusted-execution-required", "unsupported"]
    memory: Literal["enforced-job", "enforced-process", "trusted-execution-required", "unsupported"]
    child_count: Literal["enforced-job", "trusted-execution-required", "unsupported"]
    output_size: Literal["enforced", "trusted-execution-required", "unsupported"]
    read_only_input: Literal["enforced", "detected", "unsupported"]
    network_denied: Literal["enforced", "trusted-execution-required", "unsupported"]


@dataclass(frozen=True, slots=True)
class AnalyzerLimits:
    timeout_seconds: float = 120.0
    memory_bytes: int = 2 * 1024 * 1024 * 1024
    child_count: int = 8
    max_output_bytes: int = 64 * 1024 * 1024
    max_open_files: int = 256
    cpu_seconds: int = 120


def filesystem_run_id(run_id: str) -> str:
    digest = run_id.removeprefix("run:")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("run_id must be run:<sha256>")
    return digest
```

`run_analyzer(job, *, lease, consent_store, limits, deadline=None, cancelled=None)` verifies the lease and workspace seal before spawn, never accepts a consent callback, and checks database cancellation throughout execution.

- [ ] **Step 4: Implement platform-qualified process control**

On Windows, the ctypes adapter owns `CreateProcessW` and both returned handles. It performs exactly: create Job Object; configure `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, `JOB_OBJECT_LIMIT_PROCESS_MEMORY`, and `JOB_OBJECT_LIMIT_ACTIVE_PROCESS`; call `CreateProcessW` with `CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT`; assign the still-suspended process with `AssignProcessToJobObject`; then and only then call `ResumeThread`. Any create/configure/assign failure calls `TerminateProcess`, waits, closes process/thread/job handles, and never resumes. Successful assignment reports `memory="enforced-job"` and `child_count="enforced-job"`. `CREATE_NEW_PROCESS_GROUP` alone is never treated as process-tree enforcement.

On POSIX, `Popen` runs `[sys.executable, "-I", launcher_path, limits_json, executable, *arguments]` with `start_new_session=False` and `preexec_fn=None`. The trusted launcher is this complete module:

```python
POSIX_LAUNCHER = Path(__file__).with_name("code_posix_launcher.py").resolve(strict=True)


@dataclass(frozen=True, slots=True)
class PosixLaunch:
    command: tuple[str, ...]
    popen_kwargs: Mapping[str, object]
    isolation: IsolationReport


def prepare_posix_launch(
    *, executable: Path, arguments: tuple[str, ...], limits: AnalyzerLimits,
) -> PosixLaunch:
    resolved = executable.resolve(strict=True)
    limit_map = {
        "RLIMIT_FSIZE": limits.max_output_bytes,
        "RLIMIT_NOFILE": limits.max_open_files,
        "RLIMIT_CPU": limits.cpu_seconds,
    }
    memory_name = "RLIMIT_AS" if hasattr(resource, "RLIMIT_AS") else "RLIMIT_DATA"
    if hasattr(resource, memory_name):
        limit_map[memory_name] = limits.memory_bytes
    return PosixLaunch(
        command=(
            sys.executable, "-I", str(POSIX_LAUNCHER),
            json.dumps(limit_map, sort_keys=True, separators=(",", ":")),
            str(resolved), *arguments,
        ),
        popen_kwargs={"start_new_session": False, "preexec_fn": None},
        isolation=IsolationReport(
            process_tree="enforced",
            memory="enforced-process" if memory_name in limit_map else "unsupported",
            child_count="trusted-execution-required",
            output_size="enforced",
            read_only_input="detected",
            network_denied="trusted-execution-required",
        ),
    )
```

```python
from __future__ import annotations

import json
import os
import resource
import sys

ALLOWED_LIMITS = ("RLIMIT_AS", "RLIMIT_DATA", "RLIMIT_FSIZE", "RLIMIT_NOFILE", "RLIMIT_CPU")


def _apply(name: str, requested: int) -> bool:
    identifier = getattr(resource, name, None)
    if identifier is None:
        return False
    _soft, hard = resource.getrlimit(identifier)
    effective = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
    resource.setrlimit(identifier, (effective, effective))
    return True


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        raise SystemExit("usage: code_posix_launcher.py LIMITS_JSON EXECUTABLE [ARG ...]")
    limits = json.loads(argv[1])
    if not isinstance(limits, dict) or set(limits) - set(ALLOWED_LIMITS):
        raise ValueError("invalid POSIX limit map")
    applied = {}
    for name in ALLOWED_LIMITS:
        value = limits.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"invalid {name}")
        applied[name] = _apply(name, value)
    os.setsid()
    os.write(2, (json.dumps({"applied_limits": applied}, sort_keys=True) + "\n").encode("ascii"))
    os.execve(argv[2], argv[2:], dict(os.environ))
    return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

The parent uses `os.killpg(launcher_pid, signal)` after the launcher creates its session and reports readiness. `RLIMIT_AS`/`RLIMIT_DATA` are reported only as per-process memory enforcement. `RLIMIT_NPROC` is user-wide rather than a per-job descendant count and is neither set nor advertised as child-count enforcement. POSIX child count is `trusted-execution-required` unless a separately qualified platform primitive exists. Unenforceable network/read-only controls also require explicit weaker-boundary consent before spawn.

- [ ] **Step 5: Verify output and receipt after process exit**

Reverify workspace seal, output path containment/no-follow identity, output size/hash, analyzer exit, lease validity, and cancellation state. Write a canonical redacted receipt containing all identity hashes, exact isolation report, source manifest, output hash, start/end, outcome, return code, and retention deadline. Transactionally update `running -> analyzed` with receipt path/hash and retention; `analyzed` remains nonterminal and has no publication generation. A failed verification transitions to `failed|cancelled|rejected` and cannot return a publishable receipt.

- [ ] **Step 6: Run runner and consent suites and verify GREEN**

Run: `uv run pytest tests/test_code_runner.py tests/test_code_posix_launcher.py tests/test_code_consent.py tests/test_audit_fixes.py -q`

Expected: PASS. Platform-specific enforced assertions run only where the control was actually qualified; other branches assert the exact degraded label.

- [ ] **Step 7: Commit Task 7**

```bash
git add scripts/code_runner.py scripts/code_posix_launcher.py scripts/code_consent.py scripts/secret_redact.py tests/test_code_runner.py tests/test_code_posix_launcher.py
git commit -m "feat: run analyzers with qualified isolation"
```

### Task 8: Normalize SCIP v0.9 And Completed LSP Snapshots Exactly

**Files:**
- Create: `scripts/scip_ingest.py`
- Create: `scripts/lsp_snapshot.py`
- Create: `tests/test_scip_ingest.py`
- Create: `tests/test_lsp_snapshot.py`
- Create: `tests/fixtures/code_kernel/scip-python-v0.9.json`
- Create: `tests/fixtures/code_kernel/scip-valid-output-v0.9.json`

- [ ] **Step 1: Write failing per-document encoding and typed-range tests**

```python
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_scip_uses_each_documents_position_encoding(snapshot, encoding: str) -> None:
    payload = scip_payload_for_unicode_identifier(encoding)
    analysis = ingest_scip_payload(payload, snapshot=snapshot, run=make_run(snapshot))
    claim = next(item for item in analysis.symbols if item.display_name == "rocket")
    assert source_bytes(snapshot, claim.source_id)[claim.range.byte_start:claim.range.byte_end] == b"rocket"


def test_typed_range_precedes_equivalent_legacy_range(snapshot) -> None:
    payload = payload_with_typed_and_legacy_ranges(equivalent=True)
    assert ingest_scip_payload(payload, snapshot=snapshot, run=make_run(snapshot)).symbols


@pytest.mark.parametrize("damage", ["unspecified-encoding", "invalid-encoding", "bad-typed-range",
                                     "legacy-two", "legacy-five", "typed-legacy-disagree"])
def test_invalid_scip_position_contract_fails_closed(snapshot, damage: str) -> None:
    with pytest.raises(ValueError, match="position|range|encoding"):
        ingest_scip_payload(damaged_scip_payload(damage), snapshot=snapshot, run=make_run(snapshot))


SCIP_CASES = Path(__file__).parent / "fixtures/code_kernel/scip-python-v0.9.json"


def _scip_case(name: str) -> dict[str, object]:
    envelope = json.loads(SCIP_CASES.read_text(encoding="utf-8"))
    return copy.deepcopy(envelope["cases"][name])


def scip_payload_for_unicode_identifier(encoding: str) -> dict[str, object]:
    return _scip_case(f"unicode-{encoding}")


def payload_with_typed_and_legacy_ranges(*, equivalent: bool) -> dict[str, object]:
    return _scip_case("typed-legacy-equal" if equivalent else "typed-legacy-disagree")


def damaged_scip_payload(damage: str) -> dict[str, object]:
    return _scip_case(f"damaged-{damage}")


def test_standalone_scip_analyzer_output_is_valid(snapshot) -> None:
    payload = json.loads(
        (SCIP_CASES.parent / "scip-valid-output-v0.9.json").read_text(encoding="utf-8")
    )
    assert ingest_scip_payload(payload, snapshot=snapshot, run=make_run(snapshot)).symbols
```

- [ ] **Step 2: Run SCIP/LSP tests and verify RED**

Run: `uv run pytest tests/test_scip_ingest.py tests/test_lsp_snapshot.py -q`

Expected: FAIL because the normalizers do not exist.

- [ ] **Step 3: Implement bounded strict SCIP JSON reads and position conversion**

```python
def ingest_scip_json(
    path: Path,
    *,
    snapshot: CorpusSnapshot,
    run: AnalysisRun,
    limits: ScipLimits = ScipLimits(),
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> NormalizedAnalysis:
    raw = read_stable_bytes(path, limits.max_payload_bytes, label="SCIP payload")
    text = raw.decode("utf-8", errors="strict")
    payload = json.loads(text, parse_constant=reject_nonfinite)
    return ingest_scip_payload(payload, snapshot=snapshot, run=run, limits=limits,
                               deadline=deadline, cancelled=cancelled)


def ingest_scip_payload(
    payload: object,
    *,
    snapshot: CorpusSnapshot,
    run: AnalysisRun,
    limits: ScipLimits = ScipLimits(),
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> NormalizedAnalysis:
    """Validate one already-decoded bounded SCIP v0.9 payload."""
```

Each document must specify UTF-8/16/32. Prefer `single_line_range` or `multi_line_range`; if typed and legacy are both present, require semantic equality. Accept legacy only at exact length three or four. Reject unspecified encoding, clamping, line-end normalization ambiguity, surrogate/code-unit splits, unknown paths, document text differing from captured bytes, and out-of-bounds ranges. Preserve SCIP global strings exactly and scope local symbols to run, source, and revision.

`tests/fixtures/code_kernel/scip-python-v0.9.json` is a test envelope with one top-level `cases` object containing every exact payload key used above; `_scip_case` returns the payload itself, never the envelope.

- [ ] **Step 4: Normalize all relationships and diagnostics**

Map occurrences and `SymbolInformation.relationships` to definitions, declarations, references, imports, type definitions, inheritance, and implementations. Preserve unresolved/ambiguous links as `RelationshipClaim`; do not create fake nodes or unresolved v2 assertions. Normalize diagnostics and related locations with exact ranges.

- [ ] **Step 5: Implement the LSP completed-payload boundary with consistent cancellation**

`normalize_lsp_snapshot(payload, *, snapshot, run, deadline=None, cancelled=None)` and every helper called beneath it accepts and checks the same absolute deadline and cancellation callback. The payload records negotiated encoding explicitly; omission is accepted as UTF-16 only when the captured initialize result is present and omits `positionEncoding`, matching LSP 3.18. No function starts, registers, or retains a server process.

- [ ] **Step 6: Run SCIP/LSP suites and verify GREEN**

Run: `uv run pytest tests/test_scip_ingest.py tests/test_lsp_snapshot.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 8**

```bash
git add scripts/scip_ingest.py scripts/lsp_snapshot.py tests/test_scip_ingest.py tests/test_lsp_snapshot.py tests/fixtures/code_kernel/scip-python-v0.9.json tests/fixtures/code_kernel/scip-valid-output-v0.9.json
git commit -m "feat: normalize scip and lsp evidence"
```

### Task 9: Add An Interpreter-Identity-Bearing Python Analyzer

**Files:**
- Create: `scripts/python_analyzer.py`
- Create: `tests/test_python_analyzer.py`
- Modify: `.github/workflows/tests.yml:37-84`

- [ ] **Step 1: Write failing interpreter qualification and semantic tests**

```python
def test_python_analyzer_binds_exact_cpython_interpreter(snapshot) -> None:
    analysis = analyze_python(snapshot, scope=make_analysis_scope(snapshot))
    assert analysis.run.analyzer_family == "cpython-ast-symtable"
    assert analysis.run.analyzer_version == platform.python_version()
    assert analysis.run.identity.sdk_sha256 == cpython_sdk_sha256()
    assert analysis.run.qualified is (
        sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 10)
    )


def test_python_analyzer_extracts_imports_types_inheritance_and_unresolved_calls(snapshot) -> None:
    analysis = analyze_python(snapshot, scope=make_analysis_scope(snapshot))
    relations = {(item.relation, item.resolution) for item in analysis.relationships}
    assert ("IMPORTS", "resolved") in relations
    assert ("INHERITS", "resolved") in relations
    assert ("HAS_TYPE", "resolved") in relations
    assert ("CALLS", "unresolved") in relations
```

- [ ] **Step 2: Run analyzer tests and verify RED**

Run: `uv run pytest tests/test_python_analyzer.py -q`

Expected: FAIL because `python_analyzer.py` does not exist.

- [ ] **Step 3: Implement exact interpreter identity and Python 3.10 grammar parsing**

Use `ast.parse(source, filename, "exec", type_comments=True, feature_version=(3, 10))` and `symtable.symtable(source, filename, "exec")`. AST byte offsets are used only after strict UTF-8 capture. The run identity includes implementation, full version, cache tag, ABI flags, executable hash, and Python 3.10 feature version. Only actual CPython 3.10 sets `qualified=True`; other CI interpreters exercise compatibility and emit `analyzer_support="unqualified"`, preventing precise marketing or closed negatives.

- [ ] **Step 4: Implement conservative capability coverage**

Emit definitions, declarations, references, imports, calls, annotation types, type definitions, and inheritance when exactly resolvable. Emit implementations only where a captured class directly and unambiguously implements a captured abstract/protocol base; otherwise unresolved or unsupported. Reflection, dynamic imports, monkey patching, star imports, dynamic attributes, and missing dependencies remain open-world. Emit one terminal coverage row for every expected source and capability.

- [ ] **Step 5: Add a dedicated CPython 3.10 qualification CI step**

Keep the existing 3.10/3.13 cross-platform matrix. Add a named `Python kernel qualification` command conditioned on `matrix.python == '3.10'` that runs `tests/test_python_analyzer.py tests/test_scip_ingest.py`; the 3.13 jobs run compatibility tests but cannot satisfy the qualification assertion.

- [ ] **Step 6: Run analyzer tests and Ruff and verify GREEN**

Run: `uv run pytest tests/test_python_analyzer.py -q`

Expected: PASS with qualification matching the current interpreter exactly.

Run: `uv run ruff check scripts/python_analyzer.py tests/test_python_analyzer.py`

Expected: PASS.

- [ ] **Step 7: Commit Task 9**

```bash
git add scripts/python_analyzer.py tests/test_python_analyzer.py .github/workflows/tests.yml
git commit -m "feat: add qualified python syntax analyzer"
```

### Task 10: Publish Only Internally Verified v3 Slices And Reuse Valid Parent-v3 Slices

**Files:**
- Modify: `scripts/evidence_graph_builder.py:119-176,395-end`
- Modify: `scripts/code_extractor.py:120-171,289-507,1471-end`
- Modify: `scripts/generation_catalog.py:915-1092,1146-1475`
- Modify: `tests/test_evidence_graph_builder.py`
- Modify: `tests/test_evidence_graph_incremental.py`
- Modify: `tests/test_generation_catalog.py`
- Modify: `tests/code_kernel_helpers.py`
- Create: `tests/test_code_slice_publication.py`

- [ ] **Step 1: Write failing publication and parent-copy tests**

```python
@pytest.fixture
def v3_parent(catalog, snapshot, scope) -> BuildResult:
    analysis = make_normalized_analysis(snapshot, make_analysis_scope(snapshot))
    return publish_v3_fixture(catalog, snapshot, scope, (analysis,), activate=True)


@pytest.fixture
def v2_parent(catalog, snapshot, scope) -> BuildResult:
    return publish_v2_fixture(catalog, snapshot, scope, activate=True)


@pytest.fixture
def child_snapshot(snapshot: CorpusSnapshot) -> CorpusSnapshot:
    return snapshot


@pytest.fixture
def changed_snapshot(repository: Path) -> CorpusSnapshot:
    (repository / "pkg/api.py").write_text("class Changed:\n    pass\n", encoding="utf-8")
    return capture(repository)


def test_raw_precise_analysis_cannot_enter_builder(catalog, snapshot, scope) -> None:
    with pytest.raises(TypeError, match="VerifiedAnalysisBatch"):
        build_full_generation(catalog, snapshot=snapshot, repository_scope=scope,
                              graph_schema=GraphSchema.V3,
                              verified_analyses=(make_normalized_analysis(snapshot, scope),))


def test_raw_normalized_analysis_cannot_enter_public_graph_writer(tmp_path: Path) -> None:
    records = basic_graph_records()
    raw = make_normalized_analysis_for_records(records)
    with pytest.raises(TypeError, match="VerifiedAnalysisBatch"):
        create_generation_database(
            tmp_path / "raw-rejected.sqlite3",
            schema=GraphSchema.V3,
            verified_analyses=(raw,),
            **records,
        )


def test_partial_run_retains_only_fingerprint_equal_parent_v3_slice(v3_parent, child_snapshot) -> None:
    result = build_child_with_partial_run(v3_parent, child_snapshot)
    assert result.slice_report.retained == ("definitions:default:default",)
    assert result.slice_report.rejected == ()


def test_changed_source_rejects_parent_slice(v3_parent, changed_snapshot) -> None:
    result = build_child_with_failed_run(v3_parent, changed_snapshot)
    assert result.slice_report.retained == ()
    assert result.slice_report.rejected_reasons == ("source_manifest_changed",)


def test_v2_parent_contributes_source_bytes_only(v2_parent, snapshot) -> None:
    result = build_v3_child(v2_parent, snapshot)
    assert result.incremental_manifest["schema_boundary_rebuild"] is True
    assert result.slice_report.retained == ()
```

- [ ] **Step 2: Run slice tests and verify RED**

Run: `uv run pytest tests/test_code_slice_publication.py -q`

Expected: FAIL because the receipt-backed compiler verifier, public raw-analysis rejection, and bounded parent-slice copy do not exist yet.

- [ ] **Step 3: Enforce the internal verified-analysis construction invariant**

The public production signatures of `create_generation_database` and `build_full_generation` accept only `tuple[VerifiedAnalysisBatch, ...]` and perform an exact `type(item) is VerifiedAnalysisBatch` check before reading fields, so raw inputs fail with the intended `TypeError`. They reject `NormalizedAnalysis`, subclasses, and mappings. `_mint_verified_compiler_batch` requires a verified receipt, exact output/source-manifest/analysis identity hashes, and the current lease fields and sets `analysis_mode='precise'`. `verify_native_analysis` sets `analysis_mode='native-syntax'`, forces `evidence_level=syntax`, clears receipt/consent fields, and rejects compiler/semantic claims. Both paths must pass `GraphSchema.V3` explicitly. Only `build_full_generation` additionally requires `code_capture` at the manifest/publication boundary; `create_generation_database` has no such parameter. This is an internal capability/invariant against accidental bypass, not cryptographic protection from arbitrary code already executing inside the process.

Add this dependency test proving no production raw construction path exists:

```python
def test_production_modules_do_not_import_raw_v3_fixture_writer() -> None:
    offenders = []
    for path in (ROOT / "scripts").glob("*.py"):
        text = path.read_text("utf-8")
        if "_create_v3_fixture_database" in text or "_TEST_ONLY_V3_MINT" in text:
            offenders.append(path.name)
    assert offenders == []
```

- [ ] **Step 4: Implement bounded validated parent-v3 selected-slice copy**

Open only the explicitly supplied, catalog-validated parent generation. Require same repository and checkout, Graph v3, unchanged source membership/hashes for every slice source, equal analyzer semantics, manifest/lockfile/SDK/target/configuration/feature/invocation/environment/dependency hashes, equal position encoding, and a selected/current parent slice. Copy bounded rows in dependency order: run, scope, coverage, symbol claims, relationship claims, diagnostics/related locations, activation, validity. Recompute foreign-key and row-count checks. A partial/failed/cancelled/absent new run may retain such a slice; a complete empty slice replaces it. Never scan arbitrary old generations.

- [ ] **Step 5: Preserve validate-register and staged CAS publication**

Build v3 in an unpublished generation, validate exact schema, FTS, source manifest, hashes, graph ranges, copied-slice provenance, and live source recapture, then register without activation. Refactor the existing `activate` and `_activate_validated` transaction body into one `_stage_validated_activation` core; both existing APIs and the new `_stage_activation` must call that core. Do not duplicate a shortened activation implementation.

The shared core preserves all current safety in this order: full registered manifest/repository-scope/schema validation and canonical manifest bytes before locking; seal-capability acquisition; `BEGIN IMMEDIATE`; cancellation and deadline checks after the writer lock; exact active-pointer CAS read; exact `(manifest_json, manifest_sha256)` registration comparison; `MAX_ACTIVATION_HISTORY` capacity before a new history row; seal revalidation; cancellation and deadline checks immediately before the pointer write; conditional pointer update with row-count CAS; history insert; catalog byte-ceiling validation; and final cancellation/deadline/seal checks before commit. Any exception rolls back. `activate` preserves its current `False` result on CAS mismatch by translating `ConcurrentActivation`; `_stage_activation` raises it for the orchestrator.

```python
@dataclass(slots=True)
class StagedActivation:
    catalog: GenerationCatalog
    database: sqlite3.Connection
    generation_id: str
    seal_capability: _GenerationSealCapability
    deadline: float | None
    cancelled: Callable[[], bool] | None
    finished: bool = False

    def commit(self) -> None:
        if self.finished:
            raise RuntimeError("staged activation is already finished")
        try:
            _check_cancelled(self.cancelled)
            self.catalog._check_deadline(self.deadline)
            if not self.seal_capability.revalidate():
                raise ValueError("generation changed before staged commit")
            self.catalog._require_catalog_bytes(self.database)
            self.database.commit()
        except BaseException:
            self.database.rollback()
            raise
        finally:
            self.database.close()
            self.seal_capability.close()
            self.finished = True

    def rollback(self) -> None:
        if not self.finished:
            self.database.rollback()
            self.database.close()
            self.seal_capability.close()
            self.finished = True

    def __enter__(self) -> StagedActivation:
        return self

    def __exit__(self, exc_type, _exc, _tb) -> None:
        if exc_type is not None or not self.finished:
            self.rollback()


def _stage_activation(
    self,
    generation_id: str,
    *,
    expected_active: str | None,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> StagedActivation:
    candidate = self._validated_candidate_for_registered_generation(
        generation_id, deadline=deadline, cancelled=cancelled,
    )
    return self._stage_validated_activation(
        candidate, expected_active=expected_active,
        deadline=deadline, cancelled=cancelled,
    )


def _stage_validated_activation(
    self,
    candidate: _ValidatedCandidate,
    *,
    expected_active: str | None,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> StagedActivation:
    _manifest, encoded, seal_capability = self._candidate_payload(
        candidate, deadline=deadline, cancelled=cancelled,
    )
    identifier = candidate.generation_id
    expected = None if expected_active is None else _generation_id(expected_active)
    registration_token = (encoded, sha256_bytes(encoded))
    database: sqlite3.Connection | None = None
    try:
        database = self._connect(deadline=deadline)
        database.execute("BEGIN IMMEDIATE")
        _check_cancelled(cancelled)
        self._check_deadline(deadline)
        active = database.execute(
            "SELECT active_generation_id FROM catalog_state WHERE singleton=1"
        ).fetchone()
        if active is None or active["active_generation_id"] != expected:
            raise ConcurrentActivation("active generation changed")
        registered = database.execute(
            "SELECT manifest_json,manifest_sha256 FROM generations WHERE generation_id=?",
            (identifier,),
        ).fetchone()
        if registered is None or (
            bytes(registered["manifest_json"]), registered["manifest_sha256"]
        ) != registration_token:
            raise ValueError("registered generation changed before staged CAS")
        if identifier != expected:
            self._require_capacity(
                database, "activation_history", MAX_ACTIVATION_HISTORY, "history"
            )
        if not seal_capability.revalidate():
            raise ValueError("registered generation changed before activation")
        _check_cancelled(cancelled)
        self._check_deadline(deadline)
        if identifier != expected:
            cursor = database.execute(
                "UPDATE catalog_state SET active_generation_id=? "
                "WHERE singleton=1 AND active_generation_id IS ?",
                (identifier, expected),
            )
            if cursor.rowcount != 1:
                raise ConcurrentActivation("active generation changed")
            database.execute(
                "INSERT INTO activation_history(generation_id,activated_at) VALUES (?,?)",
                (identifier, _utc_timestamp(self._clock)),
            )
        self._require_catalog_bytes(database)
        _check_cancelled(cancelled)
        self._check_deadline(deadline)
        if not seal_capability.revalidate():
            raise ValueError("generation changed after staged pointer write")
        return StagedActivation(
            self, database, identifier, seal_capability, deadline, cancelled
        )
    except BaseException:
        if database is not None:
            database.rollback()
            database.close()
        seal_capability.close()
        raise
```

Add behavioral preservation tests, not source-presence assertions:

```python
def test_staged_activation_rechecks_cancel_after_writer_lock(
    catalog: GenerationCatalog, registered_generation: BuildResult, monkeypatch,
) -> None:
    writer_locked = threading.Event()
    real_connect = catalog._connect

    class TrackingConnection:
        def __init__(self, database):
            self.database = database

        def execute(self, sql, parameters=()):
            result = self.database.execute(sql, parameters)
            if sql == "BEGIN IMMEDIATE":
                writer_locked.set()
            return result

        def __getattr__(self, name):
            return getattr(self.database, name)

    monkeypatch.setattr(
        catalog, "_connect",
        lambda **kwargs: TrackingConnection(real_connect(**kwargs)),
    )
    with pytest.raises(TimeoutError, match="cancel"):
        catalog._stage_activation(
            registered_generation.generation_id, expected_active=None,
            deadline=time.monotonic() + 10,
            cancelled=lambda: writer_locked.is_set(),
        )
    assert catalog.peek_active_id() is None


def test_staged_activation_deadline_at_prewrite_rolls_back_pointer(
    catalog: GenerationCatalog, registered_generation: BuildResult, monkeypatch,
) -> None:
    prewrite = threading.Event()
    real_capacity = catalog._require_capacity

    def mark_prewrite(*args, **kwargs):
        result = real_capacity(*args, **kwargs)
        prewrite.set()
        return result

    monkeypatch.setattr(catalog, "_require_capacity", mark_prewrite)
    monkeypatch.setattr(
        catalog, "_check_deadline",
        lambda _deadline: (_ for _ in ()).throw(TimeoutError("deadline"))
        if prewrite.is_set() else None,
    )
    with pytest.raises(TimeoutError):
        catalog._stage_activation(
            registered_generation.generation_id, expected_active=None,
            deadline=time.monotonic() + 10, cancelled=None,
        )
    assert catalog.peek_active_id() is None


def test_staged_activation_preserves_history_ceiling(
    catalog: GenerationCatalog, registered_generations: tuple[BuildResult, BuildResult],
    monkeypatch,
) -> None:
    first, second = registered_generations
    assert catalog.activate(first.generation_id, expected_active=None)
    monkeypatch.setattr(generation_catalog, "MAX_ACTIVATION_HISTORY", 1)
    with pytest.raises(ValueError, match="history row ceiling"):
        catalog._stage_activation(
            second.generation_id, expected_active=first.generation_id,
            deadline=None, cancelled=None,
        )
    assert catalog.peek_active_id() == first.generation_id
```

Add `registered_generation` and `registered_generations` to `tests/code_kernel_helpers.py` in this task; they call `build_fixture_generation` with distinct explicit generation IDs and `activate=False`, so no later API is required. Also retain the existing catalog byte-ceiling, registration-byte mutation, repository-scope mismatch, schema mismatch, seal mutation, CAS mismatch, and rollback tests against both `activate` and `_stage_activation`. `StagedActivation.commit()` and `.rollback()` close the transaction and seal capability exactly once; no read path participates.

- [ ] **Step 6: Run builder, incremental, recovery, and slice suites and verify GREEN**

Run: `uv run pytest tests/test_code_slice_publication.py tests/test_evidence_graph_builder.py tests/test_evidence_graph_incremental.py tests/test_evidence_graph_recovery.py tests/test_generation_catalog.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 10**

```bash
git add scripts/evidence_graph_builder.py scripts/code_extractor.py scripts/generation_catalog.py tests/code_kernel_helpers.py tests/test_code_slice_publication.py tests/test_evidence_graph_builder.py tests/test_evidence_graph_incremental.py tests/test_generation_catalog.py
git commit -m "feat: publish verified analysis slices"
```

### Task 11: Add The Sole Supported Capture-To-Publication Orchestration API

**Files:**
- Create: `scripts/code_orchestrator.py`
- Create: `tests/test_code_orchestrator.py`
- Modify: `scripts/code_runner.py`
- Modify: `scripts/code_consent.py`
- Modify: `scripts/evidence_graph_builder.py`
- Modify: `scripts/generation_catalog.py`
- Modify: `tests/code_kernel_helpers.py`

- [ ] **Step 1: Write failing end-to-end and receipt-forgery tests**

```python
def test_precise_orchestration_connects_every_verified_boundary(repository, state_root) -> None:
    request = IndexRequest.python_scip(repository, command=fixture_scip_command())
    grant_exact_request(state_root, request)
    result = index_repository(request, state_root=state_root)
    assert result.activated is True
    assert result.graph_schema == GraphSchema.V3
    assert result.run_receipt_sha256 is not None
    assert result.consent_grant_id is not None
    with ConsentStore(state_root) as store:
        assert store.job_state(result.run_id) == "completed"
        row = store.job(result.run_id)
        assert row.source_manifest_sha256 == result.manifest["source_manifest_sha256"]
        assert row.analysis_sha256 is not None


def reject_call(label: str):
    def rejected(*_args, **_kwargs):
        raise AssertionError(f"{label} must not be called")

    return rejected


@pytest.mark.parametrize("consent_state", ["missing", "revoked"])
def test_missing_or_denied_consent_uses_lease_free_native_syntax_without_spawn(
    repository, state_root, consent_state, monkeypatch,
) -> None:
    request = IndexRequest.python_scip(repository, command=fixture_scip_command())
    if consent_state == "revoked":
        grant_exact_request(state_root, request)
        revoke_with_separate_store(state_root, request, threading.Event())
    monkeypatch.setattr(code_orchestrator, "run_analyzer", reject_call("precise runner"))
    monkeypatch.setattr(ConsentStore, "acquire_start", reject_call("consent lease"))
    result = index_repository(request, state_root=state_root)
    assert result.analysis_mode == "native-syntax"
    assert result.graph_schema == GraphSchema.V3
    assert result.run_receipt_sha256 is None
    assert result.consent_grant_id is None
    with ConsentStore(state_root) as store:
        row = store.job(result.run_id)
        assert row.state == "completed"
        assert row.source_manifest_sha256 == result.manifest["source_manifest_sha256"]
        assert row.analysis_sha256 is not None


def test_absent_analyzer_uses_native_syntax_without_process_execution(
    repository, state_root, monkeypatch,
) -> None:
    request = IndexRequest.python_scip(
        repository, command=(str(repository / "missing-analyzer"), "--index")
    )
    grant_exact_request(state_root, request)
    monkeypatch.setattr(subprocess, "Popen", reject_call("subprocess"))
    monkeypatch.setattr(ConsentStore, "acquire_start", reject_call("consent lease"))
    result = index_repository(request, state_root=state_root)
    assert result.analysis_mode == "native-syntax"
    assert (result.run_receipt_sha256, result.consent_grant_id) == (None, None)


def test_available_analyzer_with_exact_consent_requires_receipt_and_grant(
    repository, state_root,
) -> None:
    request = IndexRequest.python_scip(repository, command=fixture_scip_command())
    grant_exact_request(state_root, request)
    result = index_repository(request, state_root=state_root)
    assert result.analysis_mode == "precise"
    assert result.run_receipt_sha256 is not None
    assert result.consent_grant_id is not None


def test_index_repository_invokes_recovery_before_capture(
    repository, state_root, monkeypatch,
) -> None:
    events = []
    real_recover = code_orchestrator.recover_analyzer_jobs
    real_capture = code_orchestrator.collect_repository_code

    def recover_first(*args, **kwargs):
        events.append("recover")
        return real_recover(*args, **kwargs)

    def capture_second(*args, **kwargs):
        assert events == ["recover"]
        events.append("capture")
        return real_capture(*args, **kwargs)

    monkeypatch.setattr(code_orchestrator, "recover_analyzer_jobs", recover_first)
    monkeypatch.setattr(code_orchestrator, "collect_repository_code", capture_second)
    index_repository(
        IndexRequest.python_scip(repository, command=("missing-analyzer",)),
        state_root=state_root,
    )
    assert events[:2] == ["recover", "capture"]


def test_forged_receipt_or_mismatched_snapshot_never_publishes(repository, state_root) -> None:
    request = IndexRequest.python_scip(repository, command=fixture_scip_command())
    grant_exact_request(state_root, request)
    with pytest.raises(ReceiptVerificationError):
        _index_repository_for_test(
            request, state_root=state_root, mint=_TEST_MINT,
            receipt_mutator=flip_output_hash_for_test,
        )
    assert GenerationCatalog(state_root).get_active() is None


def test_revoke_committed_immediately_before_activation_prevents_publication(
    repository, state_root, activation_barriers,
) -> None:
    request = IndexRequest.python_scip(repository, command=fixture_scip_command())
    grant_exact_request(state_root, request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publishing = pool.submit(
            _index_repository_for_test, request, state_root=state_root,
            mint=_TEST_MINT, hooks=activation_barriers.hooks(),
        )
        activation_barriers.before_stage.wait()
        with ConsentStore(state_root) as revoker:
            revoker.revoke(consent_request_for(request))
        activation_barriers.release_before_stage.set()
        with pytest.raises(PermissionError, match="revoked"):
            publishing.result(timeout=10)
    assert GenerationCatalog(state_root).get_active() is None


def test_revoke_committed_after_staged_cas_before_consent_gate_rolls_back_catalog(
    repository, state_root, activation_barriers,
) -> None:
    request = IndexRequest.python_scip(repository, command=fixture_scip_command())
    grant_exact_request(state_root, request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publishing = pool.submit(
            _index_repository_for_test, request, state_root=state_root,
            mint=_TEST_MINT, hooks=activation_barriers.hooks(),
        )
        activation_barriers.after_stage.wait()
        with ConsentStore(state_root) as revoker:
            revoker.revoke(consent_request_for(request))
        activation_barriers.release_after_stage.set()
        with pytest.raises(PermissionError, match="revoked"):
            publishing.result(timeout=10)
    assert GenerationCatalog(state_root).get_active() is None


def test_revoke_cannot_commit_after_final_gate_until_catalog_commit_linearizes(
    repository, state_root, activation_barriers,
) -> None:
    request = IndexRequest.python_scip(repository, command=fixture_scip_command())
    grant_exact_request(state_root, request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publishing = pool.submit(
            _index_repository_for_test, request, state_root=state_root,
            mint=_TEST_MINT, hooks=activation_barriers.hooks(),
        )
        activation_barriers.gate_held.wait()
        revoking = pool.submit(
            revoke_with_separate_store, state_root, request,
            activation_barriers.revoke_entered,
        )
        assert activation_barriers.revoke_entered.wait(timeout=5)
        assert revoking.done() is False
        activation_barriers.release_gate.set()
        result = publishing.result(timeout=10)
        revoking.result(timeout=10)
    assert result.activated is True
```

`index_repository` has exactly the public signature in Step 3 and exposes no mutation seam. The private implementation accepts test hooks only when passed its module-identity `_TEST_MINT`; `_index_repository_for_test` is defined only in `tests/test_code_orchestrator.py` and supplies that guarded identity. Production callers of `index_repository` cannot pass hooks or a receipt mutator.

The same test module defines the deterministic hook fixture in full; `ActivationHooks` is accepted only by `_index_repository_for_test`:

```python
@dataclass
class ActivationBarriers:
    before_stage: threading.Event = field(default_factory=threading.Event)
    release_before_stage: threading.Event = field(default_factory=threading.Event)
    after_stage: threading.Event = field(default_factory=threading.Event)
    release_after_stage: threading.Event = field(default_factory=threading.Event)
    gate_held: threading.Event = field(default_factory=threading.Event)
    release_gate: threading.Event = field(default_factory=threading.Event)
    revoke_entered: threading.Event = field(default_factory=threading.Event)

    @staticmethod
    def _pause(reached: threading.Event, release: threading.Event) -> None:
        reached.set()
        if not release.wait(10):
            raise TimeoutError("activation test barrier was not released")

    def hooks(self) -> ActivationHooks:
        return ActivationHooks(
            before_stage=lambda: self._pause(self.before_stage, self.release_before_stage),
            after_stage=lambda: self._pause(self.after_stage, self.release_after_stage),
            gate_held=lambda: self._pause(self.gate_held, self.release_gate),
        )


@pytest.fixture
def activation_barriers() -> ActivationBarriers:
    return ActivationBarriers()


def revoke_with_separate_store(
    state_root: Path,
    request: IndexRequest,
    entered: threading.Event,
) -> None:
    entered.set()
    with ConsentStore(state_root) as store:
        store.revoke(consent_request_for(request))


def fixture_scip_command() -> tuple[str, ...]:
    copier = (
        "import shutil,sys;"
        "shutil.copyfile(sys.argv[1],sys.argv[2])"
    )
    source = (FIXTURE_ROOT.parent / "scip-valid-output-v0.9.json").resolve(strict=True)
    return (
        sys.executable, "-I", "-c", copier,
        str(source), "<OUTPUT>",
    )


def test_fixture_scip_command_writes_standalone_payload_cross_platform(tmp_path: Path) -> None:
    output = tmp_path / "analyzer-output.json"
    command = tuple(str(output) if value == "<OUTPUT>" else value
                    for value in fixture_scip_command())
    subprocess.run(
        command, check=True, stdin=subprocess.DEVNULL,
        capture_output=True, timeout=10, shell=False,
    )
    expected = json.loads(
        (FIXTURE_ROOT.parent / "scip-valid-output-v0.9.json").read_text("utf-8")
    )
    assert json.loads(output.read_text("utf-8")) == expected


def grant_exact_request(state_root: Path, request: IndexRequest) -> None:
    with ConsentStore(state_root) as store:
        store.grant(consent_request_for(request), accepted_weaker_boundary=True)


def _index_repository_for_test(
    request: IndexRequest,
    *,
    state_root: Path,
    mint: object,
    hooks: ActivationHooks | None = None,
    receipt_mutator: Callable[[AnalyzerReceipt], AnalyzerReceipt] | None = None,
) -> IndexBuildResult:
    if mint is not _TEST_MINT:
        raise PermissionError("test orchestration mint rejected")
    return code_orchestrator._index_repository(
        request,
        state_root=state_root,
        _test_hooks=hooks,
        _test_receipt_mutator=receipt_mutator,
        _test_mint=code_orchestrator._TEST_MINT,
    )


def flip_output_hash_for_test(receipt: AnalyzerReceipt) -> AnalyzerReceipt:
    return replace(receipt, output_sha256="f" * 64)
```

`fixture_scip_command` and `grant_exact_request` live in registered `tests/code_kernel_helpers.py`; `test_fixture_scip_command_writes_standalone_payload_cross_platform`, activation hooks, and private orchestration seams remain in `tests/test_code_orchestrator.py`.

- [ ] **Step 2: Run orchestration tests and verify RED**

Run: `uv run pytest tests/test_code_orchestrator.py -q`

Expected: FAIL because `code_orchestrator.py` does not exist.

- [ ] **Step 3: Define the supported request and orchestration API**

```python
@dataclass(frozen=True)
class IndexBuildResult(BuildResult):
    graph_schema: GraphSchema
    run_id: str
    analysis_mode: Literal["precise", "native-syntax"]
    run_receipt_sha256: str | None
    consent_grant_id: str | None


@dataclass(frozen=True, slots=True)
class IndexRequest:
    repository: Path
    analyzer_family: Literal["scip-python", "cpython-ast-symtable"]
    command: tuple[str, ...]
    roots: tuple[str, ...]
    include_globs: tuple[str, ...]
    ignore_globs: tuple[str, ...]
    target: str
    configuration: str
    features: tuple[str, ...]
    expected_generated_sources: Literal["available", "unavailable", "not-required"]
    dependency_resolution: Literal["complete", "partial", "unavailable"]

    @classmethod
    def python_scip(cls, repository: Path, *, command: tuple[str, ...]) -> IndexRequest:
        return cls(
            repository=repository,
            analyzer_family="scip-python",
            command=command,
            roots=("pkg", "tests"),
            include_globs=("**/*.py",),
            ignore_globs=("**/__pycache__/**", "**/.venv/**"),
            target="default",
            configuration="default",
            features=(),
            expected_generated_sources="not-required",
            dependency_resolution="complete",
        )


def index_repository(
    request: IndexRequest,
    *,
    state_root: Path,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> IndexBuildResult:
    """Run the supported capture, analysis, verification, and publication flow."""
```

`IndexBuildResult` is constructed only after catalog publication and operational-state completion. It preserves the existing `BuildResult` generation fields while exposing the exact run and Graph-schema identities. Its `__post_init__` requires both `run_receipt_sha256` and `consent_grant_id` for `analysis_mode='precise'`, and requires both to be `None` for `analysis_mode='native-syntax'`.

`analyzer_executable_available(command, *, sanitized_path) -> bool` performs no execution: it validates a non-empty command, checks an absolute executable with no-follow regular-file metadata, or resolves a bare name with `shutil.which` against only the sanitized PATH and then applies the same check. Relative paths containing separators are rejected. Availability does not grant consent and consent does not override absence.

- [ ] **Step 4: Implement the exact ordered orchestration**

1. Construct the catalog and call `recover_analyzer_jobs(state_root, catalog, apply=True, deadline=deadline, cancelled=cancelled)` before capture or creation of any new job; recovery failure is fail-closed and prevents a new owner from starting alongside unresolved state.
2. Resolve repository/checkout scope, capture a stable `CorpusSnapshot` with Task 5 limits, then capture manifests, lockfiles, SDK, target, configuration, features, dependency state, sanitized invocation, environment, and position encoding; compute every component hash, including `configuration_sha256`, and the aggregate analysis hash.
3. Preflight the requested analyzer executable without running it, derive the exact consent request, and query for an active exact grant. Only when both analyzer and grant are available call `acquire_start`; missing/revoked consent or an absent analyzer calls `start_native_job` instead. A query never prompts, grants, or executes a precise process implicitly.
4. For precise mode only, create `run/analyzer-runs/<filesystem-run-id>/`, seal captured bytes under `input/workspace`, and pass only that path to the bounded runner. Native mode calls the in-process CPython AST/symtable analyzer directly and must not call `subprocess`, `run_analyzer`, package restoration, build hooks, or repository commands.
5. In precise mode verify receipt identity, consent lease/revision, `source_manifest_sha256`, workspace seal, output path/hash, analyzer digest, and all fingerprint components. Native mode has no receipt or consent fields and verifies the same `source_manifest_sha256` plus `analysis_sha256` through `verify_native_analysis`.
6. Normalize SCIP output or native Python analysis. Native analysis receives a syntax-only verified batch; precise SCIP receives a receipt- and consent-backed verified batch. Both run under `AnalyzerHeartbeat` and require the exact owner fence before each state transition.
7. Build Graph v3, validate, and register it without activation. Commit `analyzer_job.state='ready_for_publication'`, generation ID, expected active ID, and `ready_at` in its own operational transaction; require prior state `analyzed`, exact owner identity, and the mode-specific receipt/lease nullability contract.
8. Call `catalog._stage_activation(...)`; its catalog `BEGIN IMMEDIATE` updates the pointer but remains uncommitted and invisible.
9. Precise mode uses a fresh `ConsentStore` connection and `begin_publication_gate(lease, owner, generation_id, expected_active)`. It executes `BEGIN IMMEDIATE`, re-reads the exact grant revision, lease, owner, heartbeat expiry, and job fields, requires `revoked_at IS NULL`, `cancel_requested=0`, and matching `ready_for_publication`, then updates to `publishing` without committing. Native mode calls `begin_native_publication_gate(job, owner, generation_id, expected_active)`, which performs the same owner/expiry/job checks but requires all consent/receipt fields null and performs no consent lookup.
10. While that operational-state transaction holds SQLite's sole writer slot, commit the staged catalog transaction. This catalog commit is the publication linearization point. Then update the job to `completed` and commit operational state. A precise revocation committed before gate acquisition is observed and rolls back the catalog transaction. A revocation attempting after a precise gate cannot commit until catalog commit, so it linearizes after publication and does not retroactively invalidate historical evidence. Native mode has no revocation gate because it executes no approved process.
11. If catalog commit fails, roll back the operational-state gate and staged catalog transaction. If the process crashes after catalog commit but before operational-state commit, rollback of the uncommitted gate leaves the durable job `ready_for_publication`; Task 11 recovery compares it to the active generation and either completes or quarantines it. This is ordered recovery across two databases, not a claim of cross-database atomic commit.

All publishers acquire write transactions in one order: catalog first, operational consent/job state second. Revocation writes only consent/job state. Recovery never holds write transactions on both databases simultaneously. This removes a catalog/consent lock cycle while preserving the final mode-specific gate.

Use this exact gate API:

```python
@contextmanager
def begin_publication_gate(
    self,
    lease: ConsentLease,
    owner: ProcessOwner,
    *,
    generation_id: str,
    expected_active: str | None,
) -> Iterator[PublicationGate]:
    self.database.execute("BEGIN IMMEDIATE")
    try:
        row = self._exact_live_lease_row(lease, owner)
        now = self._clock().isoformat()
        if row["state"] != "ready_for_publication":
            raise PermissionError("job is not ready for publication")
        if row["revoked_at"] is not None or row["cancel_requested"] != 0:
            raise PermissionError("consent was revoked before publication")
        if row["lease_expires_at"] <= now:
            raise LeaseLost("job owner lease expired before publication")
        if (row["publication_generation_id"], row["publication_expected_active"]) != (
            generation_id, expected_active
        ):
            raise PermissionError("publication identity does not match lease")
        cursor = self.database.execute(
            "UPDATE analyzer_job SET state='publishing',publication_started_at=? "
            "WHERE run_id=? AND lease_id=? AND state='ready_for_publication' "
            "AND owner_pid=? AND owner_start=? AND lease_expires_at>? "
            "AND cancel_requested=0",
            (now, lease.run_id, lease.lease_id,
             owner.owner_pid, owner.owner_start, now),
        )
        if cursor.rowcount != 1:
            raise PermissionError("publication job changed before gate")
        gate = PublicationGate(
            self.database, lease.run_id, lease.lease_id, generation_id,
            owner, self._clock,
        )
        yield gate
        if not gate.completed:
            self.database.rollback()
            raise RuntimeError("publication gate exited before catalog commit completion")
    except BaseException:
        self.database.rollback()
        raise


@dataclass(slots=True)
class PublicationGate:
    database: sqlite3.Connection
    run_id: str
    lease_id: str | None
    generation_id: str
    owner: ProcessOwner
    clock: Callable[[], datetime]
    completed: bool = False

    def complete_after_catalog_commit(self) -> None:
        if self.completed:
            raise RuntimeError("publication gate already completed")
        terminal_at = self.clock().isoformat()
        cursor = self.database.execute(
            "UPDATE analyzer_job SET state='completed', terminal_at=? "
            "WHERE run_id=? AND lease_id IS ? AND publication_generation_id=? "
            "AND owner_pid=? AND owner_start=? AND state='publishing' "
            "AND lease_expires_at>? AND cancel_requested=0",
            (terminal_at, self.run_id, self.lease_id, self.generation_id,
             self.owner.owner_pid, self.owner.owner_start, terminal_at),
        )
        if cursor.rowcount != 1:
            raise PermissionError("publication lease changed before completion")
        self.database.commit()
        self.completed = True
```

`begin_native_publication_gate` calls the same private `_begin_job_publication_gate` state/owner/CAS implementation with `analysis_mode='native-syntax'`, `lease_id=None`, and `require_live_grant=False`; it additionally requires `grant_id`, `grant_revision`, `receipt_path`, and `receipt_sha256` to be null. The precise wrapper passes `analysis_mode='precise'`, the exact lease, and `require_live_grant=True`. The orchestrator calls `PublicationGate.complete_after_catalog_commit()` only after `StagedActivation.commit()` returns. Completion SQL uses `lease_id IS ?` so the native null lease matches safely rather than relying on `= NULL`.

- [ ] **Step 5: Recover every abandoned analyzer state idempotently**

```python
@dataclass(frozen=True, slots=True)
class AnalyzerRecoveryFinding:
    run_id: str
    prior_state: str
    owner_pid: int
    owner_start: str
    heartbeat_at: str
    lease_expires_at: str
    owner_status: Literal["live", "dead", "expired", "invalid"]
    action: Literal["none", "failed", "rejected", "completed", "quarantined"]
    reason: str


@dataclass(frozen=True, slots=True)
class AnalyzerRecoveryReport:
    findings: tuple[AnalyzerRecoveryFinding, ...]
    changed: tuple[str, ...]
    already_terminal: tuple[str, ...]


PUBLICATION_EVIDENCE_FIELDS = (
    "run_id", "analysis_mode", "lease_id", "consent_grant_id", "consent_revision",
    "receipt_sha256", "publication_generation_id", "repository_id",
    "checkout_id", "source_manifest_sha256", "analysis_sha256",
)


def recover_analyzer_jobs(
    state_root: Path,
    catalog: GenerationCatalog,
    *,
    apply: bool,
    now: datetime | None = None,
    process_alive: Callable[[int], bool] = is_process_alive,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> AnalyzerRecoveryReport:
    """Inspect or CAS-apply bounded recovery for every analyzer job state."""
    observed_at = utc_now() if now is None else require_aware_utc(now)
    state_path = consent_state_path(state_root)
    if not state_path.exists():
        return AnalyzerRecoveryReport(findings=(), changed=(), already_terminal=())
    active = catalog.select_active_publication_evidence(
        fields=PUBLICATION_EVIDENCE_FIELDS,
        deadline=deadline,
        cancelled=cancelled,
    )
    findings: list[AnalyzerRecoveryFinding] = []
    changed: list[str] = []
    already_terminal: list[str] = []
    with ConsentStore(state_root, read_only=not apply) as store:
        if apply:
            store.database.execute("BEGIN IMMEDIATE")
        rows = store._bounded_recovery_rows()
        for row in rows:
            if row["state"] in TERMINAL_ANALYZER_STATES:
                already_terminal.append(row["run_id"])
                continue
            finding = classify_analyzer_recovery(
                row, active=active, now=observed_at, process_alive=process_alive,
            )
            findings.append(finding)
            if apply and finding.action != "none":
                if not store.apply_recovery_finding(row, finding, active=active, now=observed_at):
                    raise ConcurrentAnalyzerUpdate(row["run_id"])
                changed.append(row["run_id"])
        if apply:
            if catalog.peek_active_id(deadline=deadline) != (
                None if active is None else active["generation_id"]
            ):
                store.database.rollback()
                raise ConcurrentActivation("active generation changed during recovery")
            store.database.commit()
    return AnalyzerRecoveryReport(
        findings=tuple(findings), changed=tuple(changed),
        already_terminal=tuple(already_terminal),
    )
```

This API and its records live in `scripts/code_consent.py`, so both `code_orchestrator.py` and `doctor.py` use the same implementation. `code_consent.py` may import `GenerationCatalog`; `generation_catalog.py` must not import consent or orchestration, preventing a cycle.

`classify_analyzer_recovery` first validates owner fields and timestamps. `owner_status='live'` requires an unexpired lease and `process_alive(owner_pid)`. For `ready_for_publication`, exact matching active publication evidence takes priority and yields idempotent completion because catalog commit already linearized; otherwise a live nonterminal row has `action='none'`. Expiry is authoritative even if that PID exists, and a dead PID is abandoned even before expiry. Invalid owner/timestamp data is quarantined.

For abandoned valid rows, transitions are exact: `starting|running -> failed` with `owner_abandoned_before_analysis`; `analyzed -> quarantined` with `owner_abandoned_after_analysis`; and `publishing -> quarantined` because publishing must never be durable. For `ready_for_publication`, matching active generation and all eleven evidence fields yields `completed` with `publication_started_at=active.activated_at`, including native rows whose lease/consent/receipt fields are null. A precise revoked row with no matching commit is `rejected`. If active generation still equals `publication_expected_active`, catalog commit did not occur and the abandoned row is `failed` with `publication_not_committed`; any other active-ID or evidence mismatch is quarantined. Post-catalog revocation does not undo matching historical publication.

`_bounded_recovery_rows` uses a `LEFT JOIN` to `consent_grant` so native rows remain visible, validates `user_version`, and caps all rows. `apply_recovery_finding` uses an exact CAS over `run_id`, prior state, owner PID/start, heartbeat, and lease expiry; it sets terminal fields and never rewrites a terminal row. `apply=False` opens operational state read-only and reports the same action/reason without mutation. Repeated inspection is stable; repeated apply reports the now-terminal run in `already_terminal`. No recovery path deletes receipts, consent, or quarantine.

Add these deterministic crash tests:

```python
@pytest.fixture
def crash_state(state_root: Path, repository: Path) -> CrashPublicationState:
    return CrashPublicationState.create(state_root=state_root, repository=repository)


def seed_catalog_committed_consent_pending(
    state: CrashPublicationState,
    *,
    mismatch_field: str | None = None,
) -> None:
    state.seed_ready_job()
    state.activate_matching_generation(mismatch_field=mismatch_field)


def seed_ready_job_without_catalog_commit(state: CrashPublicationState) -> None:
    state.seed_ready_job()


def recovery_action(report: AnalyzerRecoveryReport, run_id: str) -> str:
    return next(item.action for item in report.findings if item.run_id == run_id)


def test_recovery_completes_catalog_committed_job_and_is_idempotent(crash_state) -> None:
    seed_catalog_committed_consent_pending(crash_state)
    first = recover_analyzer_jobs(
        crash_state.state_root, crash_state.catalog, apply=True,
        process_alive=lambda _pid: False,
    )
    second = recover_analyzer_jobs(
        crash_state.state_root, crash_state.catalog, apply=True,
        process_alive=lambda _pid: False,
    )
    assert recovery_action(first, crash_state.run_id) == "completed"
    assert first.changed == (crash_state.run_id,)
    assert second.already_terminal == (crash_state.run_id,)
    assert crash_state.job_state() == "completed"


def test_recovery_completes_matching_publication_even_if_revoked_after_catalog_commit(
    crash_state,
) -> None:
    seed_catalog_committed_consent_pending(crash_state)
    crash_state.revoke_after_catalog_commit()
    report = recover_analyzer_jobs(
        crash_state.state_root, crash_state.catalog, apply=True,
        process_alive=lambda _pid: False,
    )
    assert recovery_action(report, crash_state.run_id) == "completed"


def test_recovery_completes_native_publication_with_null_consent_evidence(crash_state) -> None:
    crash_state.seed_ready_job(analysis_mode="native-syntax")
    crash_state.activate_matching_generation(analysis_mode="native-syntax")
    report = recover_analyzer_jobs(
        crash_state.state_root, crash_state.catalog, apply=True,
        process_alive=lambda _pid: False,
    )
    assert recovery_action(report, crash_state.run_id) == "completed"
    assert crash_state.job_state() == "completed"


@pytest.mark.parametrize("field", [
    "analysis_mode", "lease_id", "consent_grant_id", "consent_revision", "receipt_sha256",
    "repository_id", "checkout_id", "source_manifest_sha256", "analysis_sha256",
])
def test_recovery_quarantines_active_generation_evidence_mismatch(crash_state, field) -> None:
    seed_catalog_committed_consent_pending(crash_state, mismatch_field=field)
    report = recover_analyzer_jobs(
        crash_state.state_root, crash_state.catalog, apply=True,
        process_alive=lambda _pid: False,
    )
    assert recovery_action(report, crash_state.run_id) == "quarantined"
    assert crash_state.job_state() == "quarantined"


def test_recovery_quarantines_ready_job_when_different_generation_is_active(
    crash_state,
) -> None:
    crash_state.seed_ready_job()
    crash_state.activate_unrelated_generation()
    report = recover_analyzer_jobs(
        crash_state.state_root, crash_state.catalog, apply=True,
        process_alive=lambda _pid: False,
    )
    assert recovery_action(report, crash_state.run_id) == "quarantined"
    assert crash_state.job_state() == "quarantined"


def test_recovery_rejects_revoked_job_when_generation_never_became_active(crash_state) -> None:
    seed_ready_job_without_catalog_commit(crash_state)
    crash_state.revoke_after_catalog_commit()
    report = recover_analyzer_jobs(
        crash_state.state_root, crash_state.catalog, apply=True,
        process_alive=lambda _pid: False,
    )
    assert recovery_action(report, crash_state.run_id) == "rejected"


@pytest.mark.parametrize("state,expected", [
    ("starting", "failed"), ("running", "failed"),
    ("analyzed", "quarantined"), ("ready_for_publication", "failed"),
])
def test_abandoned_precommit_states_transition_deterministically(
    crash_state, state: str, expected: str,
) -> None:
    crash_state.seed_job(state=state, owner_lease="expired", catalog_committed=False)
    report = recover_analyzer_jobs(
        crash_state.state_root, crash_state.catalog, apply=True,
        now=crash_state.after_expiry, process_alive=lambda _pid: False,
    )
    assert recovery_action(report, crash_state.run_id) == expected
    assert crash_state.job_state() == expected


def test_live_owner_is_reported_and_never_recovered(crash_state) -> None:
    crash_state.seed_job(state="running", owner_lease="live", catalog_committed=False)
    report = recover_analyzer_jobs(
        crash_state.state_root, crash_state.catalog, apply=True,
        now=crash_state.before_expiry, process_alive=lambda pid: pid == crash_state.owner_pid,
    )
    finding = next(item for item in report.findings if item.run_id == crash_state.run_id)
    assert (finding.owner_status, finding.action) == ("live", "none")
    assert report.changed == ()
    assert crash_state.job_state() == "running"


def test_inspection_is_read_only_and_reports_abandoned_owner(crash_state) -> None:
    crash_state.seed_job(state="starting", owner_lease="expired", catalog_committed=False)
    report = recover_analyzer_jobs(
        crash_state.state_root, crash_state.catalog, apply=False,
        now=crash_state.after_expiry, process_alive=lambda _pid: False,
    )
    assert recovery_action(report, crash_state.run_id) == "failed"
    assert report.changed == ()
    assert crash_state.job_state() == "starting"


def test_invalid_or_mismatched_owner_identity_is_quarantined(crash_state) -> None:
    crash_state.seed_job(state="running", owner_lease="invalid", catalog_committed=False)
    report = recover_analyzer_jobs(
        crash_state.state_root, crash_state.catalog, apply=True,
        process_alive=lambda _pid: True,
    )
    finding = next(item for item in report.findings if item.run_id == crash_state.run_id)
    assert (finding.owner_status, finding.action) == ("invalid", "quarantined")
    assert crash_state.job_state() == "quarantined"
```

- [ ] **Step 6: Add source-race and degraded-fallback tests**

Mutate the live checkout after sealed capture: analyzer output remains bound to sealed bytes, but final live recapture rejects activation. The exact missing/revoked-consent and absent-analyzer tests in Step 1 prove native syntax publishes without acquiring consent or executing any precise/build-aware process; capability reporting says precise unavailable and no compiler/semantic row exists.

- [ ] **Step 7: Run orchestration plus security suites and verify GREEN**

Run: `uv run pytest tests/test_code_orchestrator.py tests/test_code_runner.py tests/test_code_consent.py tests/test_code_slice_publication.py -q`

Expected: PASS.

- [ ] **Step 8: Commit Task 11**

```bash
git add scripts/code_orchestrator.py scripts/code_runner.py scripts/code_consent.py scripts/evidence_graph_builder.py scripts/generation_catalog.py tests/code_kernel_helpers.py tests/test_code_orchestrator.py
git commit -m "feat: orchestrate verified code indexing"
```

### Task 12: Add Non-Mutating Store-First Queries For Every Plan A Capability

**Files:**
- Create: `scripts/code_index.py`
- Create: `tests/test_code_index.py`
- Modify: `scripts/evidence_graph.py:1212-end`
- Modify: `scripts/generation_catalog.py:1554-end`
- Modify: `tests/code_kernel_helpers.py`
- Modify: `tests/test_generation_catalog.py`

- [ ] **Step 1: Write failing non-mutating selection and capability tests**

```python
def publish_v3_lexical_fixture(
    catalog: GenerationCatalog,
    snapshot: CorpusSnapshot,
    scope: RepositoryScope,
    *,
    activate: bool,
) -> BuildResult:
    if snapshot.code_capture is None:
        raise ValueError("lexical v3 fixture requires code_capture")
    extraction = extract_code(snapshot.sources, repository_id=scope.repository_id)
    analysis_scope = make_analysis_scope(snapshot)
    analysis = make_normalized_analysis(snapshot, analysis_scope)
    verified = verify_native_analysis(snapshot, analysis)
    sources = tuple(
        {
            "source_id": source.record.logical_id,
            "relative_path": source.record.relative_path,
            "sha256": source.record.sha256,
            "size": source.record.size,
            "media_type": source.record.media_type,
            "language": source.record.language,
            "git_oid": source.record.git_oid,
        }
        for source in snapshot.sources
    )
    source_bytes = {
        source.record.logical_id: source.content for source in snapshot.sources
    }
    return build_full_generation(
        catalog,
        sources=sources,
        source_bytes=source_bytes,
        nodes=extraction.nodes,
        occurrences=extraction.occurrences,
        assertions=extraction.assertions,
        evidence=extraction.evidence,
        observations=extraction.observations,
        dependencies=(),
        generation_id=f"lexical-{snapshot.corpus_sha256[:32]}",
        graph_schema=GraphSchema.V3,
        verified_analyses=(verified,),
        code_capture=snapshot.code_capture,
        repository_scope=scope,
        snapshot=snapshot,
        publication_root=Path(scope.checkout_root),
        expected_active=catalog.peek_active_id(),
        activate=activate,
    )


@pytest.fixture
def active_v3_generation(repository: Path, catalog: GenerationCatalog) -> BuildResult:
    request = IndexRequest.python_scip(repository, command=fixture_scip_command())
    grant_exact_request(catalog.state_root, request)
    return index_repository(request, state_root=catalog.state_root)


@pytest.fixture
def active_v3_syntax_generation(repository: Path, catalog: GenerationCatalog) -> BuildResult:
    snapshot = capture(repository)
    analysis = make_normalized_analysis(snapshot, make_analysis_scope(snapshot))
    return publish_v3_fixture(
        catalog, snapshot, resolve_repository_scope(repository), (analysis,),
        activate=True,
    )


@pytest.fixture
def active_v3_lexical_generation(repository: Path, catalog: GenerationCatalog) -> BuildResult:
    snapshot = capture(repository)
    result = publish_v3_lexical_fixture(
        catalog, snapshot, resolve_repository_scope(repository), activate=True
    )
    assert result.manifest["graph_schema_version"] == "evidence-graph/v3"
    assert result.manifest["code_capture"]["membership_sha256"] == (
        snapshot.code_capture.membership_sha256
    )
    return result


@pytest.fixture
def active_v2_generation(repository: Path, catalog: GenerationCatalog) -> BuildResult:
    return publish_v2_fixture(
        catalog, capture(repository), resolve_repository_scope(repository), activate=True
    )


@pytest.fixture
def index(
    repository: Path, catalog: GenerationCatalog, active_v3_generation: BuildResult,
) -> CodeIndex:
    opened = CodeIndex.open(repository, catalog=catalog)
    assert opened is not None
    return opened


def test_query_selection_never_repairs_or_activates(catalog, repository, monkeypatch) -> None:
    def reject_mutation(*_args, **_kwargs):
        raise AssertionError("read attempted catalog mutation")

    monkeypatch.setattr(catalog, "activate", reject_mutation)
    monkeypatch.setattr(catalog, "_activate_validated", reject_mutation)
    monkeypatch.setattr(catalog, "repair_active_pointer", reject_mutation)
    index = CodeIndex.open(repository, catalog=catalog, required_capability=Capability.REFERENCES)
    assert index is not None


@pytest.mark.parametrize("method,capability", [
    ("definitions", Capability.DEFINITIONS), ("declarations", Capability.DECLARATIONS),
    ("references", Capability.REFERENCES), ("callers", Capability.CALLS),
    ("imports", Capability.IMPORTS), ("types", Capability.TYPES),
    ("type_definitions", Capability.TYPE_DEFINITIONS),
    ("inheritance", Capability.INHERITANCE),
    ("implementations", Capability.IMPLEMENTATIONS),
])
def test_query_facade_covers_plan_a_operations(index, method, capability) -> None:
    result = getattr(index, method)(SymbolQuery(name="PublicApi"), limit=20)
    assert result.capability == capability


@pytest.mark.parametrize("generation_fixture,method", [
    ("active_v3_generation", "definitions"),
    ("active_v3_syntax_generation", "definitions"),
    ("active_v3_lexical_generation", "exact_search"),
    ("active_v2_generation", "callers"),
])
def test_dirty_edit_never_returns_any_stored_tier_as_current(
    request, repository, catalog, generation_fixture: str, method: str,
) -> None:
    request.getfixturevalue(generation_fixture)
    index = CodeIndex.open(repository, catalog=catalog)
    baseline = getattr(index, method)(SymbolQuery(name="PublicApi"), limit=20)
    assert baseline.items
    active_before = catalog.peek_active_id()
    (repository / "pkg/api.py").write_text(
        "class PublicApi:\n    changed = True\n", encoding="utf-8"
    )
    result = getattr(index, method)(SymbolQuery(name="PublicApi"), limit=20)
    assert "hard-stale:source_hash_changed:pkg/api.py" in result.warnings
    assert result.items == ()
    assert catalog.peek_active_id() == active_before


@pytest.mark.parametrize("generation_fixture,method", [
    ("active_v3_generation", "definitions"),
    ("active_v3_syntax_generation", "definitions"),
    ("active_v3_lexical_generation", "exact_search"),
    ("active_v2_generation", "callers"),
])
def test_missing_and_extra_candidates_make_all_stored_tiers_noncurrent(
    request, repository, catalog, generation_fixture: str, method: str,
) -> None:
    request.getfixturevalue(generation_fixture)
    baseline = getattr(CodeIndex.open(repository, catalog=catalog), method)(
        SymbolQuery(name="PublicApi"), limit=20
    )
    assert baseline.items
    (repository / "pkg/service.py").unlink()
    (repository / "pkg/new.py").write_text("value = 1\n", encoding="utf-8")
    result = getattr(CodeIndex.open(repository, catalog=catalog), method)(
        SymbolQuery(name="PublicApi"), limit=20
    )
    assert set(result.warnings) == {
        "hard-stale:source_missing:pkg/service.py",
        "hard-stale:source_membership_changed:pkg/new.py",
        "hard-stale:directory_membership_changed:pkg",
    }
    assert result.items == ()


def test_directory_membership_change_is_hard_stale_until_explicit_historical_mode(
    repository, catalog, active_v3_generation,
) -> None:
    index = CodeIndex.open(repository, catalog=catalog)
    assert index.definitions(SymbolQuery(name="PublicApi"), limit=20).items
    (repository / "pkg/ignored.tmp").write_text("new member\n", encoding="utf-8")
    current = CodeIndex.open(repository, catalog=catalog).definitions(
        SymbolQuery(name="PublicApi"), limit=20
    )
    historical = CodeIndex.open(repository, catalog=catalog).definitions(
        SymbolQuery(name="PublicApi"), limit=20, mode=QueryMode.HISTORICAL
    )
    assert current.items == ()
    assert "hard-stale:directory_membership_changed:pkg" in current.warnings
    assert historical.items
    assert all(item.freshness == ValidityStatus.HARD_STALE for item in historical.items)


def test_negative_answer_is_unknown_when_full_reconciliation_is_incomplete(
    repository, catalog, active_v3_generation, monkeypatch,
) -> None:
    monkeypatch.setattr(
        code_index, "scan_directory_membership",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ReconciliationIncomplete("bound")),
    )
    result = CodeIndex.open(repository, catalog=catalog).definitions(
        SymbolQuery(name="DefinitelyAbsent"), limit=20
    )
    assert result.items == ()
    assert result.negative is False
    assert result.completeness == "unknown"
    assert result.warnings == ("reconciliation-incomplete:bound",)


def test_warm_positive_query_hashes_only_contributing_sources_twice(
    repository, catalog, active_v3_generation, monkeypatch,
) -> None:
    hashed = []
    real_hash = code_index.hash_stable_source
    monkeypatch.setattr(
        code_index, "hash_stable_source",
        lambda path, **kwargs: (hashed.append(path.relative_to(repository).as_posix()),
                                real_hash(path, **kwargs))[1],
    )
    result = CodeIndex.open(repository, catalog=catalog).definitions(
        SymbolQuery(name="PublicApi"), limit=20
    )
    assert result.items
    assert hashed == ["pkg/api.py", "pkg/api.py"]
    assert result.reconciliation.full_content_hashes == 0
    assert result.reconciliation.selective_content_hashes == 2
```

`publish_v3_lexical_fixture` and the five fixture definitions at the start of this block are added to registered `tests/code_kernel_helpers.py`; the test functions remain in `tests/test_code_index.py`. The lexical helper uses the production extractor, native verifier, v3 builder, `code_capture`, manifest validation, registration, and optional activation path. It never calls a raw writer or constructs Graph rows through test-only SQL.

- [ ] **Step 2: Run code-index tests and verify RED**

Run: `uv run pytest tests/test_code_index.py -q`

Expected: FAIL because `code_index.py` does not exist.

- [ ] **Step 3: Separate read selection from active-pointer repair**

Add `GenerationCatalog.select_for_repository(scope, *, required_capability, deadline=None, cancelled=None)` as a read-only operation. It may return the current active generation or an already registered, already validated fallback from activation history whose v2/v3 manifest actually supports the requested capability, but it never changes catalog state. Add `peek_active_id(*, deadline=None) -> str | None`, implemented as one bounded read-only `SELECT active_generation_id`; tests use it only to prove query paths do not mutate the pointer. Keep `repair_active_pointer` as an explicit doctor/maintenance mutation requiring a writer transaction. Tests assert no `activate`, `_activate_validated`, or repair call occurs from queries and assert deadline/cancellation propagation while scanning bounded fallback history.

- [ ] **Step 4: Reconcile every stored tier once per query session and selectively hash results**

`CodeIndex.open` reconstructs `RepositoryCodePolicy`, `RepositoryCodeLimits`, persisted file stats, and directory seals only from the validated `code_capture` object added in Task 5. It creates one `ReconciliationSession` for the open index. Open-time work performs bounded no-follow directory membership scans and `stat` checks for every expected source, but does not read every file body. Metadata mismatch selects content-hash candidates; metadata equality is never accepted as proof for a returned result.

```python
@dataclass(frozen=True, slots=True)
class ReconciliationStats:
    stat_checks: int
    directory_scans: int
    selective_content_hashes: int
    full_content_hashes: int


@dataclass(slots=True)
class ReconciliationSession:
    checkout_root: Path
    policy: RepositoryCodePolicy
    limits: RepositoryCodeLimits
    files_by_id: Mapping[str, PersistedCodeFile]
    files_by_path: Mapping[str, PersistedCodeFile]
    directory_seals: Mapping[str, DirectoryMembership]
    metadata_available: bool
    open_complete: bool = False
    full_expected_set_complete: bool = False
    changed: set[str] = field(default_factory=set)
    missing: set[str] = field(default_factory=set)
    extra: set[str] = field(default_factory=set)
    directory_changed: set[str] = field(default_factory=set)
    incomplete_reason: str | None = None
    stats: ReconciliationStats = ReconciliationStats(0, 0, 0, 0)

    def reconcile_open(self, *, deadline: float | None, cancelled) -> None:
        if not self.metadata_available:
            self.incomplete_reason = "capture-metadata-unavailable"
            return
        live_directories, matching_paths, candidate_directories = scan_directory_membership(
            self.checkout_root, policy=self.policy, limits=self.limits,
            deadline=deadline, cancelled=cancelled,
        )
        self.stats = replace(
            self.stats, directory_scans=len(live_directories),
        )
        expected_paths = set(self.files_by_path)
        self.extra.update(matching_paths - expected_paths)
        self.missing.update(expected_paths - matching_paths)
        for relative_path in candidate_directories:
            if live_directories.get(relative_path) != self.directory_seals.get(relative_path):
                self.directory_changed.add(relative_path)
        for relative_path in sorted(expected_paths - self.missing):
            persisted = self.files_by_path[relative_path]
            live_stat = stat_code_file_no_follow(self.checkout_root / relative_path)
            self.stats = replace(self.stats, stat_checks=self.stats.stat_checks + 1)
            if live_stat != persisted.stat:
                digest = hash_stable_source(
                    self.checkout_root / relative_path,
                    expected_stat=live_stat, limits=self.limits,
                    deadline=deadline, cancelled=cancelled,
                )
                self.stats = replace(
                    self.stats,
                    selective_content_hashes=self.stats.selective_content_hashes + 1,
                )
                if digest != persisted.sha256:
                    self.changed.add(relative_path)
        self.open_complete = True

    def verify_result_sources(
        self, source_ids: Iterable[str], *, deadline: float | None, cancelled,
    ) -> tuple[bool, tuple[str, ...]]:
        unique = tuple(sorted(set(source_ids)))
        if not self.open_complete or not self.metadata_available:
            return False, (f"reconciliation-incomplete:{self.incomplete_reason or 'open'}",)
        before: dict[str, tuple[FileStatMetadata, str]] = {}
        warnings = set(self.membership_warnings())
        for source_id in unique:
            persisted = self.files_by_id.get(source_id)
            if persisted is None:
                warnings.add(f"hard-stale:source-provenance-missing:{source_id}")
                continue
            path = self.checkout_root / persisted.relative_path
            try:
                stat_before = stat_code_file_no_follow(path)
                digest_before = hash_stable_source(
                    path, expected_stat=stat_before, limits=self.limits,
                    deadline=deadline, cancelled=cancelled,
                )
            except FileNotFoundError:
                warnings.add(f"hard-stale:source_missing:{persisted.relative_path}")
                continue
            self.stats = replace(
                self.stats,
                selective_content_hashes=self.stats.selective_content_hashes + 1,
            )
            before[source_id] = (stat_before, digest_before)
            if digest_before != persisted.sha256:
                warnings.add(f"hard-stale:source_hash_changed:{persisted.relative_path}")

        # Response boundary: every contributing file is reopened no-follow and rehashed.
        for source_id, (stat_before, digest_before) in before.items():
            persisted = self.files_by_id[source_id]
            path = self.checkout_root / persisted.relative_path
            try:
                stat_after = stat_code_file_no_follow(path)
                digest_after = hash_stable_source(
                    path, expected_stat=stat_after, limits=self.limits,
                    deadline=deadline, cancelled=cancelled,
                )
            except FileNotFoundError:
                warnings.add(f"hard-stale:source_missing:{persisted.relative_path}")
                continue
            self.stats = replace(
                self.stats,
                selective_content_hashes=self.stats.selective_content_hashes + 1,
            )
            if (stat_after, digest_after) != (stat_before, digest_before):
                warnings.add(f"hard-stale:source_changed_during_query:{persisted.relative_path}")
        return not warnings, tuple(sorted(warnings))

    def ensure_full_expected_set(
        self, *, deadline: float | None, cancelled,
    ) -> tuple[bool, tuple[str, ...]]:
        if (not self.open_complete or self.changed or self.missing
                or self.extra or self.directory_changed):
            return False, tuple(sorted(self.membership_warnings())) or (
                f"reconciliation-incomplete:{self.incomplete_reason or 'open'}",
            )
        if not self.full_expected_set_complete:
            for persisted in sorted(self.files_by_id.values(), key=lambda item: item.relative_path):
                try:
                    path = self.checkout_root / persisted.relative_path
                    digest = hash_stable_source(
                        path, expected_stat=stat_code_file_no_follow(path),
                        limits=self.limits, deadline=deadline, cancelled=cancelled,
                    )
                except FileNotFoundError:
                    self.missing.add(persisted.relative_path)
                    continue
                self.stats = replace(
                    self.stats, full_content_hashes=self.stats.full_content_hashes + 1,
                )
                if digest != persisted.sha256:
                    self.changed.add(persisted.relative_path)
            self.full_expected_set_complete = not (
                self.changed or self.missing or self.extra or self.directory_changed
            )
        # A negative response rechecks all expected stats and directory seals, not all bodies twice.
        if not recheck_expected_stats_and_directories(self, deadline=deadline, cancelled=cancelled):
            self.full_expected_set_complete = False
        return self.full_expected_set_complete, tuple(sorted(self.membership_warnings()))
```

`membership_warnings` returns hard-stale warnings for changed/missing sources, every new policy-matching path, and every changed directory membership seal. It emits the directory warning even when concrete missing/extra paths explain the seal mismatch, preserving both diagnostics. New files can add competing definitions or relationships, so membership changes invalidate every affected repository slice rather than merely lowering confidence. `_execute_stored` is the sole path for v3 compiler/semantic, v3 syntax, v3 lexical, and v2 structural rows. It materializes bounded rows first, gathers every contributing `source_id` from claims, occurrences, evidence, dependencies, and lexical hits, then calls `verify_result_sources`; a row with no source provenance is `unknown`, never current.

Every query method adds keyword-only `mode: QueryMode = QueryMode.CURRENT`. In current mode, any hard-stale source or repository-membership warning suppresses all affected slice items; no `HARD_STALE` item is returned. Only explicit `QueryMode.HISTORICAL` may expose those bounded rows, always labeled `freshness=HARD_STALE` with exact reasons and `negative=False`. Historical mode is never used by MCP normal navigation, impact, context compilation, dead-code inference, or negative answers. Legacy v2 without `code_capture` remains queryable only as `freshness=unknown` and never current.

`CodeIndex.open` catches `ReconciliationIncomplete` from bounded scan/stat work, stores its exact reason in the session, and still permits explicitly unknown/stale output; it never silently upgrades an incomplete session.

Project-wide negative/closed-world answers call `ensure_full_expected_set`. Until it succeeds in the current open session, `negative=False` and `completeness="unknown"`. The full content pass is cached only for that session and invalidated by any later stat/directory mismatch. Positive warm queries never call it and never full-capture the repository.

- [ ] **Step 5: Implement complete provenance results and all query methods**

```python
class QueryMode(str, Enum):
    CURRENT = "current"
    HISTORICAL = "historical"


@dataclass(frozen=True, slots=True)
class CodeQueryResult:
    capability: Capability
    items: tuple[CodeResult, ...]
    precise_capability: Literal["available", "unavailable"]
    negative: bool
    completeness: Literal["complete", "incomplete", "unknown"]
    warnings: tuple[str, ...]
    reconciliation: ReconciliationStats


@dataclass(frozen=True, slots=True)
class CodeResult:
    repository_id: str
    checkout_id: str
    generation_id: str
    source_path: str
    source_sha256: str
    source_manifest_sha256: str
    analysis_sha256: str
    symbol_identity: str
    display_name: str
    kind: str
    range: PositionRange
    analyzer_family: str | None
    analyzer_version: str | None
    capability: Capability
    evidence_level: EvidenceLevel
    coverage_status: CoverageStatus
    ambiguity: bool
    freshness: ValidityStatus
    stale_reason: str | None
    contributing_source_ids: tuple[str, ...]
```

Implement definitions, declarations, references, callers, callees, imports, types, type definitions, inheritance in both directions, implementations, diagnostics, capability report, and token-budgeted repository map. V3 queries use normalized claims directly. V2 retains existing `EvidenceGraph` structural readers for callers/callees/dependencies/paths/architecture until equivalent normalized evidence exists; precise-only operations return `CapabilityUnavailable`, never synthetic v3 coverage.

- [ ] **Step 6: Enforce honest negatives and bounded ambiguity**

Negative is true only when both `closed_world` from Task 3 and `ReconciliationSession.ensure_full_expected_set` succeed for the current open session. Partial, unsupported, unqualified, generated-unavailable, dependency-incomplete, ambiguous, soft/hard-stale, legacy-metadata-missing, reconciliation-incomplete, or v2 results are non-negative with `completeness="unknown"` and warnings. Equivalent top candidates remain bounded and unselected.

- [ ] **Step 7: Run query/catalog suites and verify GREEN**

Run: `uv run pytest tests/test_code_index.py tests/test_generation_catalog.py tests/test_evidence_graph.py -q`

Expected: PASS.

- [ ] **Step 8: Commit Task 12**

```bash
git add scripts/code_index.py scripts/evidence_graph.py scripts/generation_catalog.py tests/code_kernel_helpers.py tests/test_code_index.py tests/test_generation_catalog.py
git commit -m "feat: query code evidence without catalog mutation"
```

### Task 13: Integrate Code Graph, Impact, And Context With Full Provenance

**Files:**
- Modify: `scripts/code_graph.py:823-end`
- Modify: `scripts/impact_analysis.py:609-931`
- Modify: `scripts/context_compiler.py:27-170,343-395,487-end`
- Modify: `tests/test_code_graph.py`
- Modify: `tests/test_impact_analysis.py`
- Modify: `tests/test_context_compiler.py`
- Modify: `tests/code_kernel_helpers.py`

- [ ] **Step 1: Write failing delegation, stale-impact, and context tests**

```python
@pytest.fixture
def active_graph(catalog: GenerationCatalog, active_v3_generation: BuildResult) -> EvidenceGraph:
    return EvidenceGraph(
        catalog.generations_path / active_v3_generation.generation_id / "evidence.sqlite3",
        state_root=catalog.state_root,
        generation_id=active_v3_generation.generation_id,
    )


@pytest.fixture
def code_result(index: CodeIndex) -> CodeResult:
    result = index.definitions(SymbolQuery(name="PublicApi"), limit=1)
    assert len(result.items) == 1
    return result.items[0]


def test_context_keeps_every_code_provenance_field(code_result: CodeResult) -> None:
    budget = ContextBudget(
        model=None, max_input_tokens=512,
        reserved_output_tokens=64, safety_margin_tokens=32,
    )
    packed = compile_code_context((code_result,), budget=budget)
    text = packed.items[0].text
    for value in (
        code_result.repository_id, code_result.checkout_id, code_result.generation_id,
        code_result.source_sha256, code_result.source_manifest_sha256,
        code_result.analysis_sha256, code_result.analyzer_family,
        code_result.analyzer_version, code_result.capability.value,
        code_result.evidence_level.value, str(code_result.ambiguity),
        code_result.freshness.value, code_result.stale_reason or "none",
    ):
        assert value in text
    assert packed.items[0].token_cost == count_tokens(text).tokens


def test_impact_never_traverses_hard_stale_relationships(repository, active_graph) -> None:
    (repository / "pkg/api.py").write_text(
        "class PublicApi:\n    changed = True\n", encoding="utf-8"
    )
    result = analyze_impact(root=repository, graph=active_graph)
    assert all(item["freshness"] != "hard_stale"
               for group in result["affected"].values() for item in group)
```

`active_graph` and `code_result` are added to registered `tests/code_kernel_helpers.py`; Task 13 includes that helper file in its commit.

- [ ] **Step 2: Run focused integration tests and verify RED**

Run: `uv run pytest tests/test_code_graph.py tests/test_impact_analysis.py tests/test_context_compiler.py -q -k "provenance or hard_stale or code_index"`

Expected: FAIL because direct graph readers and incomplete context metadata remain.

- [ ] **Step 3: Delegate stored code operations and preserve v2 fallback**

Route stored `code_graph.py` operations through `CodeIndex`, preserving explicit `live=True` and current response keys. V2 structural operations continue through the v2 reader exposed by `CodeIndex`; no operation silently loses existing v2 behavior.

- [ ] **Step 4: Integrate current validity into impact**

Map changed ranges to current symbol claims, traverse only current relationship claims, report unavailable precise capability and exact stale reason, and retain conservative textual fallback. Historical/soft-stale traversal requires an explicit non-default argument; hard-stale never answers a normal query.

- [ ] **Step 5: Pack full provenance and compute cost from the final full text**

Build the complete metadata-prefixed string first, then call `context_budget.count_tokens(text, model=budget.model)` and use the returned count. Never estimate cost from display name or body alone. Drop whole lower-ranked items rather than removing provenance fields.

- [ ] **Step 6: Run integration suites and verify GREEN**

Run: `uv run pytest tests/test_code_graph.py tests/test_impact_analysis.py tests/test_context_compiler.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 13**

```bash
git add scripts/code_graph.py scripts/impact_analysis.py scripts/context_compiler.py tests/code_kernel_helpers.py tests/test_code_graph.py tests/test_impact_analysis.py tests/test_context_compiler.py
git commit -m "feat: integrate current code evidence"
```

### Task 14: Extend Existing MCP Modes And Doctor Deletion Safety

**Files:**
- Modify: `scripts/mcp_server.py:297-464,1320-1471,1739-2060`
- Modify: `scripts/mcp_contract.py:16-107`
- Modify: `scripts/doctor.py:48-82,1301-1335,3418-3765`
- Modify: `tests/test_mcp_server.py`
- Modify: `tests/test_mcp_contract.py`
- Modify: `tests/test_doctor.py`
- Modify: `docs/STRUCTURE.md:297-308`
- Modify: `AGENTS.md:runtime deletion contract`
- Modify: `CLAUDE.md:runtime deletion contract`

- [ ] **Step 1: Write failing exact MCP schema and deletion-blocker tests**

```python
CANONICAL_TOOL_NAMES = (
    "recall", "read_page", "wiki_overview", "vault_status", "get_decisions",
    "get_context", "check_contradiction", "log_decision", "compile",
    "find_dead_code", "get_architecture", "doctor",
)


def test_mcp_schema_inventory_is_always_exactly_twelve() -> None:
    assert tuple(mcp_server.TOOL_INPUT_SCHEMAS) == CANONICAL_TOOL_NAMES


def test_mcp_built_definitions_match_twelve_when_optional_sdk_is_installed() -> None:
    if not mcp_server.MCP_AVAILABLE:
        pytest.skip("requires the mcp-server optional dependency")
    tools = mcp_server._build_tool_definitions()
    assert tuple(tool.name for tool in tools) == CANONICAL_TOOL_NAMES


def test_code_modes_require_absolute_root_and_mode_specific_arguments(tmp_path: Path) -> None:
    assert mcp_server._validate_tool_arguments(
        "get_architecture", {"directory": "relative", "mode": "definitions", "symbol": "x"}
    ) == "argument 'directory' must be an absolute non-root directory"
    absolute = str((tmp_path / "repo").resolve())
    Path(absolute).mkdir()
    assert mcp_server._validate_tool_arguments(
        "get_architecture", {"directory": absolute, "mode": "implementations"}
    ) == "required argument is missing for implementations: symbol"


def test_doctor_blocks_run_deletion_for_analyzer_state(
    state_root: Path, repository: Path,
) -> None:
    seed_doctor_analyzer_state(state_root, repository)
    report = run_doctor(root=state_root, state_root=state_root)
    codes = {item["code"] for item in report["run_deletion"]["blockers"]}
    assert {"analyzer_job_live", "analyzer_receipt_retained", "analyzer_consent_retained"} <= codes


def test_doctor_inspection_reports_abandoned_owner_without_mutation(
    state_root: Path, repository: Path,
) -> None:
    run_id = seed_abandoned_native_job(state_root, repository, state="running")
    report = run_doctor(root=state_root, state_root=state_root, repair=False)
    finding = next(item for item in report["analyzer_recovery"] if item["run_id"] == run_id)
    assert finding["owner_status"] == "expired"
    assert finding["action"] == "failed"
    with ConsentStore(state_root) as store:
        assert store.job_state(run_id) == "running"
    assert "analyzer_job_abandoned" in {
        item["code"] for item in report["run_deletion"]["blockers"]
    }


def test_doctor_repair_applies_recovery_explicitly_and_idempotently(
    state_root: Path, repository: Path,
) -> None:
    run_id = seed_abandoned_native_job(state_root, repository, state="starting")
    run_doctor(
        root=state_root, state_root=state_root, repair=True, repair_actions=set()
    )
    with ConsentStore(state_root) as store:
        assert store.job_state(run_id) == "starting"
    first = run_doctor(root=state_root, state_root=state_root, repair=True)
    second = run_doctor(root=state_root, state_root=state_root, repair=True)
    with ConsentStore(state_root) as store:
        assert store.job_state(run_id) == "failed"
    assert {item["run_id"] for item in first["analyzer_recovery_applied"]} == {run_id}
    assert second["analyzer_recovery_applied"] == []


def seed_doctor_analyzer_state(state_root: Path, repository: Path) -> None:
    request = consent_request(repository)
    with ConsentStore(state_root) as store:
        store.grant(request, accepted_weaker_boundary=True)
        lease = store.acquire_start(request, run_id="run:" + "d" * 64)
        receipt = state_root / "run/analyzer-runs" / filesystem_run_id(lease.run_id) / "receipt.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text('{"outcome":"complete"}\n', encoding="utf-8")
        store.database.execute(
            "UPDATE analyzer_job SET state='analyzed',receipt_path=?,receipt_sha256=?,retain_until=? "
            "WHERE run_id=?",
            (
                str(receipt.relative_to(state_root)), hashlib.sha256(receipt.read_bytes()).hexdigest(),
                "2999-01-01T00:00:00+00:00", lease.run_id,
            ),
        )
        store.database.commit()


def seed_abandoned_native_job(
    state_root: Path, repository: Path, *, state: Literal["starting", "running"],
) -> str:
    owner = ProcessOwner(2_000_000_000, "process-start/v1:" + "f" * 64)
    old_now = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with ConsentStore(state_root, owner=owner, clock=lambda: old_now) as store:
        job = store.start_native_job(
            native_job_request(repository), run_id="run:" + "e" * 64
        )
        if state == "running":
            store.mark_native_running(job, owner)
    return job.run_id
```

- [ ] **Step 2: Run MCP/doctor tests and verify RED**

Run: `uv run pytest tests/test_mcp_server.py tests/test_mcp_contract.py tests/test_doctor.py -q -k "code_modes or analyzer_state or schema_inventory or built_definitions"`

Expected: FAIL because modes and analyzer deletion blockers are absent.

After GREEN in the base environment, run the SDK-bearing assertion explicitly:

Run: `uv run --extra mcp-server pytest tests/test_mcp_server.py -q -k "built_definitions"`

Expected: PASS with 12 built definitions. Without the extra, the canonical schema assertion still runs and the built-definition assertion skips rather than claiming zero tools is twelve.

- [ ] **Step 3: Extend the exact existing input schema and conditional validation**

Keep the 12 `_build_tool_definitions()` entries. Add `definitions`, `declarations`, `references`, `imports`, `types`, `type-definitions`, `inheritance`, `implementations`, `diagnostics`, `capabilities`, and `repository-map` to `get_architecture.mode`. `directory` remains a string in JSON Schema but runtime validation requires an absolute existing non-filesystem-root path before dispatch. Add `allOf` conditionals requiring `symbol` for symbol operations and both `symbol`/`target` for path. Mirror these conditions in `_validate_tool_arguments`, because its subset validator does not generically execute JSON Schema `allOf`.

- [ ] **Step 4: Dispatch through CodeIndex with the current absolute deadline**

Use the resolved absolute root, pass the one MCP operation deadline/cancellation token, preserve the response envelope, and expose complete provenance in `data`. Do not add an indexing mutation to a query tool and do not add a thirteenth tool.

- [ ] **Step 5: Add bounded analyzer-state doctor checks**

Call `recover_analyzer_jobs(state_root, catalog, apply=False)` during every doctor inspection and expose each bounded owner PID/start, heartbeat, expiry, owner status, proposed action, and reason under `analyzer_recovery`. Doctor opens an existing catalog read-only; a missing/unreadable catalog produces a recovery diagnostic and no new catalog/database. Live nonterminal rows block with `analyzer_job_live`; expired/dead nonterminal rows block with `analyzer_job_abandoned`, so they are diagnosable rather than silently permanent. Only explicit `doctor --repair` opens writable state, calls the same named API with `apply=True`, and records its CAS-applied transitions under `analyzer_recovery_applied`; ordinary doctor never mutates analyzer state.

Add `analyzer_recovery` to `VALID_REPAIR_ACTIONS`. Apply mutation only when `repair=True` and that action is selected; `repair=False`, or `repair_actions` excluding it, remains read-only. The CLI reports the action in its normal repair ledger.

Continue to block deletion for `starting|running|analyzed|ready_for_publication|publishing|quarantined` jobs, any receipt whose `retain_until` is in the future, unrevoked precise consent grants, unreadable/corrupt state, unsafe run paths, or live owners. A recovered terminal native job with no receipt/consent no longer blocks solely because its old owner PID exists. Revoked grants with no retained job/receipt cease blocking after their retention contract. Scan `run/analyzer-runs/` no-follow with count/size/depth limits and reconcile each directory with its precise database row; native-syntax jobs intentionally have no analyzer scratch directory and are not reported missing. Doctor repair never deletes consent, jobs, receipts, or quarantine automatically.

- [ ] **Step 6: Update canonical deletion text and byte-identical contracts**

State that `run/` must not be deleted while analyzer jobs are live or abandoned-but-unrecovered, receipts are retained, active consent exists, quarantine exists, or analyzer state is unreadable. Document owner heartbeat diagnosis and explicit doctor repair. Keep `AGENTS.md` and `CLAUDE.md` byte-identical.

- [ ] **Step 7: Run MCP/doctor/structure suites and verify GREEN**

Run: `uv run pytest tests/test_mcp_server.py tests/test_mcp_contract.py tests/test_doctor.py tests/test_structure.py -q`

Expected: PASS with exactly 12 tools and all new deletion blockers.

- [ ] **Step 8: Commit Task 14**

```bash
git add scripts/mcp_server.py scripts/mcp_contract.py scripts/doctor.py tests/test_mcp_server.py tests/test_mcp_contract.py tests/test_doctor.py docs/STRUCTURE.md AGENTS.md CLAUDE.md
git commit -m "feat: expose code queries and protect analyzer state"
```

### Task 15: Add Deterministic Correctness Metrics And Optional Qualified Latency

**Files:**
- Create: `benchmark/code-kernel-python-v1.json`
- Create: `benchmark/code-kernel-report-v1.schema.json`
- Create: `benchmark/run_code_kernel.py`
- Create: `tests/test_code_kernel_benchmark.py`
- Modify: `benchmark/COMPARATIVE.md`

- [ ] **Step 1: Write failing deterministic benchmark tests without wall-clock thresholds**

```python
def test_benchmark_ledger_covers_required_strata() -> None:
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    assert {item["stratum"] for item in ledger["queries"]} == {
        "definition", "declaration", "reference", "caller", "import", "type-definition",
        "inheritance", "implementation", "ambiguous", "absent-open", "absent-closed",
        "stale-source", "wrong-repository", "wrong-generation",
    }


def test_deterministic_report_schema_and_correctness(tmp_path: Path) -> None:
    report = run_code_kernel.run(fixture=FIXTURE, ledger=LEDGER, repetitions=1,
                                 measure_latency=False)
    validate_schema(report, REPORT_SCHEMA)
    assert report["wrong_repository_answers"] == 0
    assert report["wrong_generation_answers"] == 0
    assert report["stale_result_answers"] == 0
    assert report["citation_source_hash_validity"] == 1.0
    assert report["latency"]["qualified"] is False
    assert report["latency"]["samples_ms"] == []


def test_warm_positive_benchmark_uses_selective_not_full_reconciliation() -> None:
    report = run_code_kernel.run(
        fixture=FIXTURE, ledger=LEDGER, repetitions=3,
        measure_latency=False, query_subset="positive-result",
    )
    assert report["reconciliation"]["full_expected_set_runs"] == 0
    assert report["reconciliation"]["full_content_hashes"] == 0
    assert report["reconciliation"]["selective_content_hashes"] == (
        2 * report["reconciliation"]["unique_contributing_sources"] * 3
    )


def test_negative_benchmark_records_one_full_reconciliation_per_session() -> None:
    report = run_code_kernel.run(
        fixture=FIXTURE, ledger=LEDGER, repetitions=3,
        measure_latency=False, query_subset="absent-closed",
    )
    assert report["reconciliation"]["query_sessions"] == 1
    assert report["reconciliation"]["full_expected_set_runs"] == 1
    assert report["reconciliation"]["full_content_hashes"] == report["source_file_count"]
```

- [ ] **Step 2: Run benchmark tests and verify RED**

Run: `uv run pytest tests/test_code_kernel_benchmark.py -q`

Expected: FAIL because benchmark files do not exist.

- [ ] **Step 3: Implement portable argparse and output behavior**

```python
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Python code-kernel benchmark")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", default="-", help="'-' writes canonical JSON to stdout")
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--measure-latency", action="store_true")
    parser.add_argument("--machine-label")
    return parser.parse_args(argv)
```

No test asserts a wall-clock threshold. Deterministic tests validate schema, ranking, metrics, source hashes, stale/wrong-scope behavior, reconciliation counters, and token accounting. The report schema requires `reconciliation` with `query_sessions`, `stat_checks`, `directory_scans`, `unique_contributing_sources`, `selective_content_hashes`, `full_expected_set_runs`, and `full_content_hashes`.

- [ ] **Step 4: Separate local latency evidence from correctness gates**

With `--measure-latency`, require `--machine-label`, record OS, CPU count, Python, cold/warm state, dependency-cache state, repetitions, raw samples, p50/p95, analyzer, target/configuration, coverage, and reconciliation counters. Warm positive-result latency measures one open-time metadata reconciliation followed by selective two-boundary hashes for contributing sources; it does not full-hash or full-capture the repository per query. Closed-negative latency is reported separately and includes its one per-session full expected-set reconciliation. Mark `qualified=true` only when all metadata is present. The runner reports spec thresholds but does not let noisy CI timing fail pytest.

- [ ] **Step 5: Run deterministic pytest and a portable external report command**

Run: `uv run pytest tests/test_code_kernel_benchmark.py -q`

Expected: PASS.

Run: `uv run python benchmark/run_code_kernel.py --fixture tests/fixtures/code_kernel/python --ledger benchmark/code-kernel-python-v1.json --output - --repetitions 3`

Expected: canonical JSON on stdout, validated correctness metrics, and `latency.qualified=false`.

- [ ] **Step 6: Document the qualification boundary**

`benchmark/COMPARATIVE.md` states that deterministic fixture success is not eleven-language qualification or superiority. A dated externally saved latency report on the declared reference machine is required before latency claims.

- [ ] **Step 7: Commit Task 15**

```bash
git add benchmark/code-kernel-python-v1.json benchmark/code-kernel-report-v1.schema.json benchmark/run_code_kernel.py benchmark/COMPARATIVE.md tests/test_code_kernel_benchmark.py
git commit -m "test: add deterministic python kernel benchmark"
```

### Task 16: Document Implemented Plan A And Run Full Verification

**Files:**
- Create: `docs/CODE-KERNEL.md`
- Modify: `docs/STRUCTURE.md:117-155,213-308`
- Modify: `AGENTS.md:approved/current code-kernel text`
- Modify: `CLAUDE.md:approved/current code-kernel text`
- Modify: `tests/README.md`
- Modify: `tests/test_structure.py`

- [ ] **Step 1: Write the failing implemented-guide contract test**

```python
def test_code_kernel_guide_names_implemented_and_deferred_boundaries() -> None:
    guide = (ROOT / "docs/CODE-KERNEL.md").read_text(encoding="utf-8")
    for value in (
        "Plan A: Python foundation", "sealed captured workspace", "verified analyzer receipt",
        "exact invocation consent", "evidence-graph/v3", "v2 read compatibility",
        "lease-free native syntax fallback", "owner heartbeat", "explicit doctor repair",
        "v2-safe schema default", "non-mutating query selection",
        "hard-stale membership", "exactly 12 MCP tools",
        "Later plans: polyglot analyzers, repository topology, and signed packaging",
    ):
        assert value in guide
```

- [ ] **Step 2: Run the guide test and verify RED**

Run: `uv run pytest tests/test_structure.py::test_code_kernel_guide_names_implemented_and_deferred_boundaries -q`

Expected: FAIL because `docs/CODE-KERNEL.md` does not exist.

- [ ] **Step 3: Write the guide and move verified target statements into the current checkpoint**

Document capture policy/limits/stat/directory seals, the single `source_manifest_sha256` name, analyzed-state `analysis_sha256`, exact consent grant/revoke API, consent-free native syntax fallback, process owner/heartbeat/expiry, all recovery transitions, `analyzed -> ready_for_publication -> publishing -> completed` publication states, weaker-boundary meaning, analyzer-run layout, optional native versus required precise receipt identity, SCIP/native behavior, Python interpreter qualification, v2-default/v3-explicit Graph compatibility, low-level database writer versus manifest/publication `code_capture` boundary, optional non-code `code_capture`, session freshness for every stored evidence tier, hard membership invalidation, explicit historical mode, selective positive hashes, full negative reconciliation, scope/coverage, honest negatives, orchestration API, queries, impact/context/MCP, doctor inspection/explicit repair, offline behavior, and verification. Only now update the implemented checkpoint in `docs/STRUCTURE.md` and the agent contracts to say Plan A is implemented.

- [ ] **Step 4: Run all focused Plan A suites**

Run: `uv run pytest tests/test_code_kernel_helpers.py tests/test_code_intelligence.py tests/test_evidence_graph.py tests/test_evidence_graph_builder.py tests/test_generation_catalog.py tests/test_code_workspace.py tests/test_code_consent.py tests/test_code_runner.py tests/test_code_posix_launcher.py tests/test_scip_ingest.py tests/test_lsp_snapshot.py tests/test_python_analyzer.py tests/test_code_slice_publication.py tests/test_code_orchestrator.py tests/test_code_index.py tests/test_code_graph.py tests/test_impact_analysis.py tests/test_context_compiler.py tests/test_mcp_server.py tests/test_mcp_contract.py tests/test_doctor.py tests/test_code_kernel_benchmark.py tests/test_structure.py -q`

Expected: PASS.

Plan-authoring verification note, not an implementation allowance: on 2026-07-21 the current pre-Plan-A worktree's fail-fast baseline stopped at `tests/test_code_extractor.py:534::test_tree_sitter_languages_extract_when_available_and_degrade_when_absent` with `1 failed, 366 passed, 5 skipped`; TypeScript extraction returned only file/module/repository nodes. This unrelated observation must be rechecked or fixed separately. It does not change the future gate in Step 5: Task 16 still requires `uv run pytest -q` to exit zero.

- [ ] **Step 5: Run full repository verification**

Run: `uv run ruff check scripts/ tests/ benchmark/`

Expected: PASS.

Run: `uv run pytest -q`

Expected: PASS.

Run: `uv run python scripts/lint_memory.py --scope all --fail-on-findings --allowed-categories orphan_daily_logs missing_backlinks missing_sources_section temporal_validity stale_compiled`

Expected: PASS with no new blocking category.

Run: `uv run python scripts/lint_memory.py --scope all`

Expected: no more than the recorded 60 findings and none from the new decision or guide; plain lint need not exit zero.

Run: `uv run pytest tests/test_readme_i18n.py -q`

Expected: PASS; no README file is edited by Plan A.

Run: `uv run --extra mcp-server pytest tests/test_mcp_server.py -q -k "built_definitions"`

Expected: PASS with exactly 12 SDK-built definitions; the base suite separately proves the 12 canonical schemas without the optional SDK.

- [ ] **Step 6: Verify direct safety invariants**

Run: `uv run pytest tests/test_evidence_graph.py tests/test_evidence_graph_builder.py tests/test_generation_catalog.py tests/test_code_consent.py tests/test_code_runner.py tests/test_code_posix_launcher.py tests/test_code_orchestrator.py tests/test_code_index.py tests/test_mcp_server.py tests/test_doctor.py -q -k "user_version or journal_mode or synchronous or wal or mutation or reparse or revoke or activation or history or recovery or heartbeat or owner or native or source_manifest or analysis_sha256 or lexical_fixture or code_capture or dirty_edit or missing_and_extra or directory_membership or historical or incomplete or selective or twelve or deletion or suspended or preexec"`

Expected: PASS with rollback/FULL/no-WAL, sealed-input race rejection, precise/native branch separation, owner recovery, full activation fencing, hard membership invalidation, 12 canonical tool schemas, and analyzer deletion blockers.

- [ ] **Step 7: Commit Task 16**

```bash
git add docs/CODE-KERNEL.md docs/STRUCTURE.md AGENTS.md CLAUDE.md tests/README.md tests/test_structure.py
git commit -m "docs: document python code kernel foundation"
```

## Final Self-Review: Review Finding Mapping

1. Supported orchestration branches: Task 11 runs sealed capture -> exact consent -> bounded runner -> verified receipt for precise analysis, or sealed capture -> lease-free in-process native verification when consent/analyzer is unavailable; both publish only verified batches and raw precise batches are rejected in Task 10.
2. Complete freshness identity and expected scope for honest negatives: Task 3 uses `source_manifest_sha256` and `analysis_sha256` in run/scope/batch/receipt contracts; Task 4 persists both in Graph; Tasks 6/11 persist and compare both in operational state and recovery.
3. Sealed captured workspace, mutation detection, read-only/degraded labels: Task 5 Steps 3-4, Task 7 Steps 3-5, Task 11 Steps 4-5.
4. Doctor deletion contract for jobs, receipts, and consent: Task 14 Steps 1 and 5-7.
5. Non-mutating capability selection separate from repair: Task 12 Steps 1 and 3.
6. Bounded validated parent-v3 slice copy and source-only v2 migration: Task 10 Steps 1, 4, and 6.
7. Imports, types, type definitions, inheritance, implementations, and v2 preservation: Task 3 Capability enum; Task 8 normalization; Task 9 analysis; Task 12 Steps 4-5.
8. Full `CodeResult` and context provenance with full-text token cost: Task 12 Step 4 and Task 13 Steps 1 and 5.
9. Correct existing helper, SQLite, MCP, and absolute-root APIs: Task 6 Step 3 uses `validate_state_root` as `None`-returning and `open_operational_db(..., busy_ms=...)`; Task 4 Step 4 sets/checks `user_version`; Task 14 uses `_build_tool_definitions`, absolute roots, and explicit conditional validation.
10. Security and race tests plus honest platform controls: Task 5 Step 1, Task 6 Step 1, Task 7 Steps 1 and 3-6, Task 11 Steps 1 and 5.
11. Exact normalized relationship table, unresolved representation, validity, and related diagnostics: Task 3 Step 3 and Task 4 Step 3.
12. Safe Graph schema compatibility and independently green schema task: Task 4 Steps 1-6 keep omitted legacy calls on v2 while precise/v3 callers pass v3 explicitly; database `user_version` and manifest identity always match and no global v3 stamp exists.
13. SCIP per-document encodings, typed-first ranges, strict stable JSON, and cancellation: Task 8 Steps 1 and 3-6.
14. CPython 3.10 or interpreter identity and CI qualification: Task 9 Steps 1, 3, and 5.
15. Task 1 target/current truth and memory-lint baseline behavior: Task 1 Steps 1, 4-6.
16. Exact shared fixture/helper file: Task 2, used by every later test task.
17. External capture limits, roots/globs/ignore/suffix/NFC/casefold/link/reparse/UTF-8/chunks/platform tests: Task 5 Steps 1 and 3-5.
18. Deterministic pytest metrics, no timing thresholds, qualified external latency, portable output: Task 15 Steps 1 and 3-6.
19. Consistent LSP helper cancellation/deadline arguments: Task 8 Step 5.
20. Independently green tasks with no later-only APIs: Tasks are ordered fixtures -> contracts -> schema -> capture -> consent -> runner -> normalizers -> analyzer -> publication -> orchestration -> queries -> integrations -> MCP/doctor -> benchmark -> docs. Each task runs its own complete affected suites before its single commit.
21. Query-time live source truth: Task 12 Steps 1 and 4 use one open-session stat/directory reconciliation, selective two-boundary hashes for every source contributing v3 precise, v3 syntax/lexical, or v2 structural results, and one cached full expected-set pass only for negatives; dirty/missing/extra/incomplete tests prove no old fact is current and the active pointer is unchanged.
22. Publication linearization without false cross-database atomicity: Task 6 defines exact precise consent leases, native null-consent rows, owner fences, and separate-connection revocation; Task 11 stages an uncommitted catalog CAS, acquires the mode-appropriate operational-state gate, and makes catalog commit the linearization point with deterministic races.
23. Complete Graph v3 extension DDL: Task 4 Step 3 contains all ten required tables plus `run_capability`, 19 extension indexes, enum checks, FKs, exact-one validity subjects, selected-slice uniqueness, and a fixed full-schema digest test.
24. Reconstructible closed-world state: Task 4 Step 3 persists and validates expected scope count/hash including each scope's exact `source_manifest_sha256`, target/configuration rows, expected source count/hash/membership, generated/dependency state, and exact coverage; missing, duplicate, extra, manifest-mismatched, and hash-damaged rows fail closed.
25. Platform process entry ordering: Task 7 Steps 1 and 4 use Windows `CREATE_SUSPENDED` -> configured Job Object assignment -> `ResumeThread`, with failure tests proving no entrypoint execution; POSIX uses the complete trusted launcher and no `preexec_fn`, and never calls `RLIMIT_NPROC` a per-job child cap.
26. Shared fixture discovery: Task 2 modifies root `tests/conftest.py` with `pytest_plugins = ("tests.code_kernel_helpers",)`, matching the existing `tests/__init__.py` package layout, and proves `state_root` and `repository` in Task 2's independently green test.
27. No production raw-analysis bypass: Task 3 defines native verification, Task 4 builds schema fixtures through the production verified writer, and Task 10 proves public Graph/builder paths reject `NormalizedAnalysis` while no script contains a raw writer or test mint.
28. Real SQLite concurrency: Task 6's races instantiate one `ConsentStore` per worker and retain the exact `validate_state_root(...) -> None` and `open_operational_db(..., busy_ms=5_000)` APIs.
29. Current lint baseline: Tasks 1 and 16 allow `stale_compiled` alongside the recorded 58 backlink findings and existing orphan daily finding, then cap plain lint at the recorded 60 total with no new-file findings.
30. Optional MCP SDK truth: Task 14 always checks the canonical 12 `TOOL_INPUT_SCHEMAS`, skips built definitions without the SDK, and runs a separate `--extra mcp-server` assertion for exactly 12 built tools.
31. Explicit v3 and compatible v2 schema selection: Task 4 gives legacy APIs the sole safe default `GraphSchema.V2`, rejects verified batches under v2, and requires every precise production path to pass v3 explicitly; each task introduces its own fixtures/APIs before use and runs its complete affected suite before one planned commit.
32. Optional closed persisted capture contract: Task 5 Step 4 adds exact policy, every limit, file stat/hash rows, directory membership seals, and `membership_sha256` at `build_full_generation`/manifest publication when code analysis/v3 slices are requested; the low-level `create_generation_database` has no duplicate argument, ordinary non-code corpus/doctor paths omit it validly, and legacy code metadata can never be current.
33. No full-capture query loop: Task 12 Step 4 records open-time metadata work, hashes only contributing positive-result sources twice, and performs one per-session full hash only when a negative answer requests closed-world completeness; Task 15 asserts these counters and separates positive/negative latency.
34. All stored evidence tiers share freshness: Task 12 first proves positive baseline results, then parameterizes dirty edits and membership changes across v3 compiler/semantic, v3 syntax, v3 lexical, and v2 structural fixtures through the sole `_execute_stored` path; new matching files and directory seal changes are hard stale, normal mode suppresses them, and only explicit historical mode may expose labeled rows.
35. Production Graph has no raw construction path: Task 4 schema tests use `verify_native_analysis` plus the manifest-agnostic `create_generation_database`; Task 10 scans every script for forbidden raw writer/token names, public APIs reject `NormalizedAnalysis`, and publication adds `code_capture` only through `build_full_generation`.
36. SCIP orchestration fixture truth: Task 8 creates a standalone valid output file; Task 11 uses an absolute-path, `shell=False`, `sys.executable -I -c` copier and behaviorally compares its output, never feeding the cases envelope to ingestion.
37. Publication state truth: Task 6 DDL and Tasks 7/11 transitions distinguish precise receipt-bearing and native receipt-free states; `analysis_sha256` is null in `starting|running`, mandatory from `analyzed` onward, rechecked before ready, and required with the publication generation after catalog commit.
38. Registered fixture ownership: Task 12 defines `publish_v3_lexical_fixture` fully in `tests/code_kernel_helpers.py` through native verification and production v3 publication with `code_capture`; Task 13 adds `code_result` and both commits include the helper file.
39. Executable crash recovery: Tasks 6 and 11 define PID/start owner identity, heartbeat and expiry, bounded `recover_analyzer_jobs`, exact `source_manifest_sha256` and `analysis_sha256` evidence comparison, idempotent post-catalog completion, and live/abandoned/mismatch tests; `index_repository` applies it at startup and doctor inspects read-only or applies only under explicit repair.
40. Lease-free native fallback: Task 11 branches before `acquire_start`; missing/revoked consent or an absent analyzer creates a native job, runs only in-process AST/symtable analysis, and publishes syntax with null receipt/grant fields. Precise mode requires both fields and the subprocess receipt boundary.
41. Activation safety preservation: Task 10 refactors `activate`, `_activate_validated`, and `_stage_activation` onto one core retaining registered bytes, repository scope/schema, seal, CAS, cancellation/deadline, history ceiling, catalog byte ceiling, and rollback checks, with focused cancellation/deadline/history tests.
42. Future verification remains strict: Task 16's focused and full implementation gates require zero failures and do not add an exception or xfail allowance.
43. One source-manifest name: operational, Graph, receipt, batch, lease, and recovery contracts all use `source_manifest_sha256`; no alternate snapshot-named digest exists, and constraint/mismatch tests enforce the identity.
44. Analysis identity state transition: `analyzer_job.analysis_sha256` is nullable only before analysis, populated atomically by both precise and native analyzed transitions, required through ready/completed, and compared against active Graph evidence during recovery.
45. Lexical fixture completeness: Task 12's registered helper constructs sources, extraction records, a verified native batch, v3 generation, `code_capture`, registration, and activation only through production APIs; no undefined lexical helper remains.

## Explicit Out Of Scope

Later approved plans cover precise analyzers and independent qualification for JavaScript, TypeScript, Java, C#, Go, Rust, C, C++, PHP, and Ruby; repository topology for SQL, package managers, routes, Docker/Compose, Kubernetes/Kustomize, Terraform, CI, generated sources, and deployment edges; and signed analyzer packaging with binary hashes, licenses, SBOMs, platform installers, restoration workflows, and release distribution. Plan A does not claim full Phase 2 replacement, does not add a watcher or daemon, does not package analyzer binaries, and does not change the MCP tool count.
