# Python/Pyright Read-Only Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a production-ready, Python-only, read-only LSP navigation path through pinned Pyright 1.1.411 and the existing `get_architecture` MCP tool without adding a semantic result cache, graph, tool, daemon, or runtime root.

**Architecture:** Keep Evidence Graph and Tree-sitter as the structural discovery and fallback path. Add an owned Python 3.10-compatible stdlib LSP client that launches one bounded Pyright process lazily per repository inside the MCP process, proves freshness by pre/post workspace revisions, normalizes facts behind one facade, and renders compact deterministic results. Pyright installation is an explicit command into the approved `cache/code-tools/pyright/1.1.411/`; process scratch and cleanup evidence use the approved `run/lsp/<owner-nonce>/` path.

**Tech Stack:** Python 3.10 standard library, LSP 3.18 over stdio JSON-RPC 2.0, Pyright 1.1.411 npm artifact, Node.js 22, Git, existing `code_intelligence.PositionEncoding` and `code_intelligence.Capability`, existing `repository_scope.RepositoryScope`, pytest, Ruff, and the existing 12-tool MCP envelope.

---

## Binding Scope

This plan implements only production-quality Python navigation through Pyright 1.1.411. It does not add Serena, SolidLSP, multilspy, Rust, an interactive SCIP path, another language profile, another graph, another MCP tool, another runtime root, a persistent daemon, or a semantic result cache. It does not publish query-time LSP facts into Evidence Graph v3. It does not route exact small results through Context Compiler; Context Compiler remains only the broad architecture, impact, grounded-QA, and multi-source packing path.

“Read-only” is an API property, not a sandbox claim. The client exposes no rename, formatting, completion, code-action, workspace-edit, arbitrary command, or other mutation operation. Pyright still runs with the current user's OS permissions and is supported only for operator-trusted local repositories. Returned repository locations are contained and validated, but Pyright may read configured interpreters, external stubs, and library code; those inputs are identity-bearing provenance. No task may claim that this is an OS sandbox or that Pyright cannot write user-accessible paths.

No task may claim market superiority. Comparative measurements may be recorded only as measurements against named pinned releases, and the public result remains “unclaimed” until a separately approved paired benchmark passes predefined gates.

## Verified Current-Practice Constraints

Verified on 2026-07-22 and binding on implementation:

- LSP 3.18 uses ASCII headers, byte-valued `Content-Length`, UTF-8 JSON content, JSON-RPC 2.0, negotiated `utf-8|utf-16|utf-32` positions, and UTF-16 when `capabilities.positionEncoding` is absent: <https://microsoft.github.io/language-server-protocol/specifications/lsp/3.18/specification/>.
- LSP cancellation sends `$/cancelRequest`; cancelled requests still receive a response, and unsupported server-to-client requests receive JSON-RPC `MethodNotFound` (`-32601`): <https://microsoft.github.io/language-server-protocol/specifications/lsp/3.18/specification/#cancelRequest>.
- Pyright 1.1.411 is the release at commit `9a9205fc32a2685767f38f348f5d9232701d4b0b`: <https://github.com/microsoft/pyright/releases/tag/1.1.411>.
- The qualified npm artifact is `https://registry.npmjs.org/pyright/-/pyright-1.1.411.tgz`, SHA-256 `bd5c488fc20fa237a944279bf32cae2f986cf10d5d5d9e8705819859daeb2f4a`, npm integrity `sha512-03S/vmS5lF1S/tVbKc2WNXCMq8JWCwta/qIYjj1jvqbQhoy+N3NgBzHTSmUlbYD6DJwqQ5XHf108QujoqeURvw==`, with binaries `pyright` and `pyright-langserver`: <https://registry.npmjs.org/pyright/1.1.411>.
- Pyright 1.1.411 declares Node `>=14.0.0`; this product profile qualifies Node major 22 and CI pins Node `22.23.1`, the current Node 22 LTS release observed on 2026-07-22: <https://nodejs.org/dist/index.json>.
- Pyright language-server configuration reads `python`, `python.analysis`, and `pyright` sections and defaults to open-files-only diagnostics; configuration delivery and fingerprinting must cover the exact values sent: <https://github.com/microsoft/pyright/blob/1.1.411/packages/pyright-internal/src/server.ts>.
- Pyright's CLI documents zero-based diagnostic positions and its Node-based distribution: <https://github.com/microsoft/pyright/blob/1.1.411/docs/command-line.md>.

Project constraints override generic upstream behavior: 8 MiB frames, 32 outstanding requests, 10,000 normalized locations, 10,000 diagnostics, 256 KiB hover content, 4 MiB stderr, JSON depth 64, one restart, a 60-second startup deadline, and the existing 10-second deadline for non-LSP MCP modes.

## Shared Public Contracts

Define these names once in the task that owns their file. Later tasks import them and must not redefine them.

```python
# scripts/code_navigation.py
class NavigationStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    NOT_READY = "not_ready"
    STALE = "stale"
    TIMEOUT = "timeout"
    ERROR = "error"


class ResolutionLabel(str, Enum):
    LSP_CONFIRMED = "lsp_confirmed"
    GRAPH_CONFIRMED = "graph_confirmed"
    LSP_AND_GRAPH = "lsp_and_graph"
    LSP_ONLY = "lsp_only"
    GRAPH_CANDIDATE = "graph_candidate"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class NavigationRequest:
    repository: RepositoryScope
    capability: Capability
    path: str
    line: int
    character: int
    offset: int = 0
    limit: int = 10
    direction: str | None = None


@dataclass(frozen=True, slots=True)
class NavigationResult:
    status: NavigationStatus
    requested_capability: Capability
    effective_capability: Capability | None
    provider: str | None
    provider_version: str | None
    repository_id: str
    checkout_id: str
    workspace_revision_before: str
    workspace_revision_after: str
    document_version: int | None
    position_encoding: PositionEncoding | None
    readiness: str
    symbol: str | None
    total: int
    offset: int
    limit: int
    locations: tuple[NavigationLocation, ...]
    diagnostics: tuple[NavigationDiagnostic, ...]
    hover: str | None
    resolution: ResolutionLabel
    provenance: tuple[Provenance, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NavigationDiagnostic:
    path: str
    range: PositionRange
    severity: DiagnosticSeverity
    code: str | None
    message: str
    related: tuple[NavigationLocation, ...]
    provenance: tuple[Provenance, ...]
```

Status semantics are closed:

- `ok`: the requested capability completed against one unchanged workspace revision; an empty provider result is still provider-reported, not closed-world proof.
- `partial`: useful provider or structural facts exist, but readiness, indexing, truncation, filtered external paths, fallback, or timeout prevents a complete claim.
- `unsupported`: the selected provider did not advertise the requested capability and no equivalent precise operation was performed.
- `not_ready`: startup or the document-symbol readiness probe did not complete; this is never rendered as an empty semantic result.
- `stale`: the workspace changed across both the initial attempt and the one allowed retry; both responses are discarded.
- `timeout`: the operation deadline elapsed after cancellation; any bounded partial facts are marked timed out.
- `error`: validated execution failed without a safe semantic result; structural fallback may still be attached with explicit provenance and `partial` instead.

Every normalized location carries repository-relative `path`, exact half-open UTF-8 byte `PositionRange`, one-based `line`, zero-based UTF-8 byte `character`, optional containing symbol and signature, resolution label, and provenance. Every result carries pre/post revision and document version. Provider references and calls are `provider_reported`; they are never represented as complete call-graph proof.

## File Map

**Create:**

- `scripts/lsp_paths.py`: approved managed-tool and owner-scratch path derivation.
- `scripts/lsp_positions.py`: UTF-8 input validation, LSP position/range conversion, and cross-platform file URI conversion.
- `scripts/workspace_revision.py`: Git and non-Git revision manifests plus create/change/rename/delete deltas.
- `scripts/lsp_protocol.py`: strict bounded framing, JSON validation, allowlisted messages, pending requests, cancellation, and generation nonces.
- `scripts/lsp_process.py`: minimal environment, lazy process ownership, startup state, stderr ring, idle shutdown, and one restart.
- `scripts/lsp_process_tree.py`: POSIX process-group and Windows Job Object lifecycle helpers.
- `scripts/lsp_security.py`: repository request/response path containment and log redaction.
- `scripts/pyright_profile.py`: pinned identity, discovery, configuration fingerprint, and capability mapping.
- `scripts/install_pyright.py`: explicit digest-verified managed installer; never called by a query.
- `scripts/pyright_session.py`: readiness, document synchronization, diagnostics, and Pyright feature requests.
- `scripts/code_navigation.py`: stable normalized facade, ambiguity, structural fallback, provenance, and freshness retry.
- `scripts/code_navigation_renderer.py`: deterministic compact rendering and stateless windows.
- `tests/test_lsp_paths.py`, `tests/test_lsp_positions.py`, `tests/test_workspace_revision.py`, `tests/test_lsp_protocol.py`, `tests/test_lsp_process.py`, `tests/test_lsp_process_tree.py`, `tests/test_lsp_security.py`, `tests/test_pyright_profile.py`, `tests/test_install_pyright.py`, `tests/test_pyright_session.py`, `tests/test_code_navigation.py`, and `tests/test_code_navigation_renderer.py`: focused unit and integration contracts.
- `tests/fake_lsp_server.py`: deterministic hostile and healthy stdio server used by protocol/process tests.
- `tests/fixtures/code_kernel/python/pkg/unicode_api.py`: Unicode position fixture.
- `tests/fixtures/code_kernel/python/pkg/rename_target.py`: deterministic rename/delete fixture.
- `tests/fixtures/code_kernel/python/pyrightconfig.json`: fixed qualified fixture settings.
- `benchmark/code-navigation-python-v1.json`: fixed gold queries and acceptance policy.
- `benchmark/code-navigation-python-v1.schema.json`: closed benchmark manifest/report schema.
- `benchmark/run_code_navigation.py`: fixture and operator-corpus benchmark runner.
- `benchmark/generate_python_qualification.py`: deterministic 100 KLOC public qualification repository generator.
- `docs/CODE-NAVIGATION.md`: operator, trust, installation, query, status, and qualification contract.

**Modify:**

- `scripts/mcp_server.py` and `tests/test_mcp_server.py`: precise `get_architecture` modes, closed argument shapes, deadlines, and backward compatibility.
- `scripts/doctor.py`, `tests/test_doctor.py`, and `tests/test_runtime_deletion_contract.py`: Pyright identity/readiness diagnosis and `run/lsp` deletion blockers.
- `scripts/memory_state.py`, `docs/STRUCTURE.md`, `AGENTS.md`, `CLAUDE.md`, and `tests/test_structure.py`: approved runtime path constants and implemented-vs-target docs.
- `tests/code_kernel_helpers.py`: one shared Pyright fixture locator and managed-package test installer helper.
- `.github/workflows/tests.yml`: cross-platform protocol and pinned real-Pyright jobs.
- `README.md`, `README.ru.md`, `README.zh-CN.md`, `docs/USER-GUIDE.md`, `docs/ARCHITECTURE.md`, `docs/operating-model.md`, `tests/README.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, and `benchmark/COMPARATIVE.md`: synchronized production behavior and honest measurement language.

Tasks are ordered by dependency. A subagent owns only the files listed in its current task. When a later task modifies an earlier file, it must begin from the committed prior task rather than carrying a private copy.

### Task 1: Lock Runtime Paths And Documentation Tests

**Files:**
- Create: `scripts/lsp_paths.py`
- Create: `tests/test_lsp_paths.py`
- Modify: `scripts/memory_state.py:70-82`
- Modify: `tests/test_structure.py:180-250`
- Modify: `docs/STRUCTURE.md:158-186,244-339`
- Modify: `AGENTS.md:140-154`
- Modify: `CLAUDE.md:140-154`

- [ ] **Step 1: Write the failing runtime path tests**

```python
def test_approved_lsp_paths_are_inside_existing_runtime_zones(tmp_path: Path) -> None:
    assert managed_pyright_root(tmp_path) == tmp_path / "cache/code-tools/pyright/1.1.411"
    assert lsp_owner_root(tmp_path, "a" * 32) == tmp_path / "run/lsp" / ("a" * 32)


@pytest.mark.parametrize("nonce", ["", "A" * 32, "a/b", "..", "a" * 31])
def test_owner_nonce_is_exact_lowercase_hex(nonce: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="owner nonce"):
        lsp_owner_root(tmp_path, nonce)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_lsp_paths.py tests/test_structure.py -q -k "lsp_paths or code_navigation"`

Expected: FAIL with `ModuleNotFoundError: No module named 'lsp_paths'`.

- [ ] **Step 3: Implement exact path derivation and sync the canonical docs**

```python
PYRIGHT_VERSION = "1.1.411"
PYRIGHT_RELATIVE_ROOT = Path("cache/code-tools/pyright") / PYRIGHT_VERSION
LSP_RELATIVE_ROOT = Path("run/lsp")
_OWNER_NONCE = re.compile(r"[0-9a-f]{32}\Z")


def managed_pyright_root(state_root: Path) -> Path:
    return Path(state_root).resolve() / PYRIGHT_RELATIVE_ROOT


def lsp_owner_root(state_root: Path, owner_nonce: str) -> Path:
    if _OWNER_NONCE.fullmatch(owner_nonce) is None:
        raise ValueError("owner nonce must be 32 lowercase hexadecimal characters")
    return Path(state_root).resolve() / LSP_RELATIVE_ROOT / owner_nonce
```

Export `CODE_TOOLS_DIR = STATE_ROOT / "cache/code-tools"` and `LSP_RUN_DIR = STATE_ROOT / "run/lsp"` from `scripts/memory_state.py`. Update the implemented section only to say the paths are reserved and helpers are implemented; do not claim navigation works before Task 15. Keep `AGENTS.md` and `CLAUDE.md` byte-identical. State that `cache/code-tools` is regenerable, while `run/lsp` follows the `run/` deletion contract.

- [ ] **Step 4: Run path, structure, and Ruff checks and verify GREEN**

Run: `uv run pytest tests/test_lsp_paths.py tests/test_structure.py -q`

Expected: PASS.

Run: `uv run ruff check scripts/lsp_paths.py scripts/memory_state.py tests/test_lsp_paths.py tests/test_structure.py`

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add scripts/lsp_paths.py scripts/memory_state.py tests/test_lsp_paths.py tests/test_structure.py docs/STRUCTURE.md AGENTS.md CLAUDE.md
git commit -m "feat: lock code navigation runtime paths"
```

### Task 2: Normalize UTF Positions And File URIs

**Files:**
- Create: `scripts/lsp_positions.py`
- Create: `tests/test_lsp_positions.py`
- Create: `tests/fixtures/code_kernel/python/pkg/unicode_api.py`

- [ ] **Step 1: Write failing Unicode, line-ending, and Windows URI tests**

```python
def test_utf8_anchor_converts_to_each_negotiated_encoding() -> None:
    document = SourceDocument.from_bytes("pkg/unicode_api.py", "a😀β\r\n".encode())
    anchor = document.validate_anchor(line=1, character=len("a😀".encode()))
    assert document.to_lsp(anchor, PositionEncoding.UTF8) == LspPosition(0, 5)
    assert document.to_lsp(anchor, PositionEncoding.UTF16) == LspPosition(0, 3)
    assert document.to_lsp(anchor, PositionEncoding.UTF32) == LspPosition(0, 2)


def test_file_uri_round_trips_windows_drive_case_and_space() -> None:
    path = PureWindowsPath(r"C:\repo name\pkg\api.py")
    uri = path_to_file_uri(path)
    assert uri == "file:///C:/repo%20name/pkg/api.py"
    assert file_uri_to_path(uri, platform="nt") == path
```

Also test invalid UTF-8, a character inside a multibyte sequence, line zero, past-end lines, offsets beyond the line, lone CR, CRLF, non-file URIs, UNC paths, encoded traversal, and LSP ranges whose end precedes start.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_lsp_positions.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'lsp_positions'`.

- [ ] **Step 3: Implement the immutable position API**

```python
@dataclass(frozen=True, slots=True)
class LspPosition:
    line: int
    character: int


@dataclass(frozen=True, slots=True)
class LspRange:
    start: LspPosition
    end: LspPosition


@dataclass(frozen=True, slots=True)
class SourceAnchor:
    path: str
    line: int
    utf8_character: int
    byte_offset: int


@dataclass(frozen=True, slots=True)
class SourceDocument:
    path: str
    content: bytes
    source_sha256: str
    line_spans: tuple[tuple[int, int], ...]
```

Expose exact methods `SourceDocument.from_bytes(cls, path: str, content: bytes) -> SourceDocument`, `SourceDocument.validate_anchor(self, *, line: int, character: int) -> SourceAnchor`, `SourceDocument.to_lsp(self, anchor: SourceAnchor, encoding: PositionEncoding) -> LspPosition`, and `SourceDocument.to_byte_range(self, value: LspRange, encoding: PositionEncoding) -> PositionRange`. `line` at the public boundary is one-based; `character` is a zero-based UTF-8 byte offset. Reject a byte offset that splits a code point. Use `code_intelligence.PositionEncoding` and return `code_intelligence.PositionRange`; do not introduce another encoding enum or byte-range type. `path_to_file_uri()` and `file_uri_to_path()` normalize Windows drive letters and percent encoding but do not decide repository containment; Task 7 owns containment.

- [ ] **Step 4: Run focused tests and Ruff and verify GREEN**

Run: `uv run pytest tests/test_lsp_positions.py tests/test_code_intelligence.py -q`

Expected: PASS.

Run: `uv run ruff check scripts/lsp_positions.py tests/test_lsp_positions.py`

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add scripts/lsp_positions.py tests/test_lsp_positions.py tests/fixtures/code_kernel/python/pkg/unicode_api.py
git commit -m "feat: normalize LSP source positions"
```

### Task 3: Prove Workspace Revisions And Deltas

**Files:**
- Create: `scripts/workspace_revision.py`
- Create: `tests/test_workspace_revision.py`
- Modify: `tests/code_kernel_helpers.py:59-125`
- Create: `tests/fixtures/code_kernel/python/pkg/rename_target.py`
- Create: `tests/fixtures/code_kernel/python/pyrightconfig.json`

- [ ] **Step 1: Write failing Git and non-Git revision tests**

```python
def test_revision_changes_for_dirty_untracked_deleted_and_config(repository: Path) -> None:
    scope = resolve_repository_scope(repository)
    before = compute_workspace_revision(scope)
    (repository / "pkg/api.py").write_text("class Changed:\n    pass\n", encoding="utf-8")
    (repository / "pkg/new.py").write_text("value = 1\n", encoding="utf-8")
    (repository / "pkg/base.py").unlink()
    (repository / "pyrightconfig.json").write_text('{"typeCheckingMode":"strict"}', encoding="utf-8")
    after = compute_workspace_revision(scope)
    assert after.revision_sha256 != before.revision_sha256
    assert {item.kind for item in after.entries} >= {"modified", "untracked", "deleted", "configuration"}


def test_delta_detects_content_identical_rename(repository: Path) -> None:
    scope = resolve_repository_scope(repository)
    before = compute_workspace_revision(scope)
    (repository / "pkg/rename_target.py").rename(repository / "pkg/renamed.py")
    after = compute_workspace_revision(scope)
    assert diff_workspace_revisions(before, after).renamed == (("pkg/rename_target.py", "pkg/renamed.py"),)
```

Test cancellation, deadline, Git command bounds, normalization collisions, relevant config files, non-Git manifests, file-count/byte ceilings, and deterministic ordering.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_workspace_revision.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'workspace_revision'`.

- [ ] **Step 3: Implement exact revision and delta contracts**

```python
PYTHON_CONFIG_NAMES = frozenset({
    ".python-version", "Pipfile", "Pipfile.lock", "poetry.lock",
    "pyproject.toml", "pyrightconfig.json", "setup.cfg", "tox.ini", "uv.lock",
})
MAX_REVISION_FILES = 100_000
MAX_REVISION_BYTES = 2 * 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RevisionEntry:
    path: str
    kind: str
    sha256: str | None
    size: int


@dataclass(frozen=True, slots=True)
class WorkspaceRevision:
    repository_id: str
    checkout_id: str
    git_head: str | None
    entries: tuple[RevisionEntry, ...]
    revision_sha256: str


@dataclass(frozen=True, slots=True)
class WorkspaceDelta:
    created: tuple[str, ...]
    changed: tuple[str, ...]
    renamed: tuple[tuple[str, str], ...]
    deleted: tuple[str, ...]
    configuration_changed: bool
```

Expose exact functions `compute_workspace_revision(repository: RepositoryScope, *, deadline: float | None = None, cancelled: Callable[[], bool] | None = None) -> WorkspaceRevision` and `diff_workspace_revisions(before: WorkspaceRevision, after: WorkspaceRevision) -> WorkspaceDelta`. Use `repository_scope.sanitized_git_environment()`. For Git, hash HEAD plus every dirty, untracked, deleted, renamed, `.py`, `.pyi`, and configuration entry reported by bounded `git status --porcelain=v2 -z --untracked-files=all`; hash current bytes for present entries and use `sha256=None,size=0` for deletions. Always include root configuration names and sorted `requirements*.txt`. For non-Git repositories, hash the bounded manifest of all `.py`, `.pyi`, and those configuration files. Rename pairing is allowed only for one deleted and one created entry with the same non-null SHA-256; ambiguous matches remain delete/create.

- [ ] **Step 4: Run revision and repository tests and verify GREEN**

Run: `uv run pytest tests/test_workspace_revision.py tests/test_repository_scope.py tests/test_code_kernel_helpers.py -q`

Expected: PASS.

Run: `uv run ruff check scripts/workspace_revision.py tests/test_workspace_revision.py tests/code_kernel_helpers.py`

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add scripts/workspace_revision.py tests/test_workspace_revision.py tests/code_kernel_helpers.py tests/fixtures/code_kernel/python/pkg/rename_target.py tests/fixtures/code_kernel/python/pyrightconfig.json
git commit -m "feat: prove live workspace revisions"
```

### Task 4: Implement The Strict Bounded LSP Protocol

**Files:**
- Create: `scripts/lsp_protocol.py`
- Create: `tests/test_lsp_protocol.py`
- Create: `tests/fake_lsp_server.py`

- [ ] **Step 1: Write failing framing and hostile-server tests**

```python
def test_frame_reader_accepts_one_strict_lsp_message() -> None:
    body = b'{"jsonrpc":"2.0","id":1,"result":null}'
    stream = io.BytesIO(b"Content-Length: 38\r\n\r\n" + body)
    assert JsonRpcFrameReader(stream).read() == {"jsonrpc": "2.0", "id": 1, "result": None}


@pytest.mark.parametrize("scenario", [
    "oversized-frame", "invalid-header", "duplicate-content-length",
    "wrong-charset", "json-depth-65", "batch-message", "duplicate-response-id",
])
def test_protocol_violation_is_fatal(scenario: str, fake_server: FakeLspServer) -> None:
    connection = fake_server.connection(scenario)
    with pytest.raises(ProtocolViolation):
        connection.request("initialize", {}, deadline=time.monotonic() + 1)
    assert connection.fatal is True
```

Also test exactly 8 MiB acceptance and 8 MiB plus one rejection, 8 KiB header ceiling, malformed UTF-8/JSON, non-2.0 messages, invalid IDs, response with both result and error, 32 active requests, request 33 rejection, 10,000 location/diagnostic ceilings, 256 KiB hover ceiling, cancellation, late generation responses, and `MethodNotFound` for unknown server requests.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_lsp_protocol.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'lsp_protocol'`.

- [ ] **Step 3: Implement exact bounds, pending identity, and allowlists**

```python
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_HEADER_BYTES = 8 * 1024
MAX_PENDING_REQUESTS = 32
MAX_LOCATIONS = 10_000
MAX_DIAGNOSTICS = 10_000
MAX_HOVER_BYTES = 256 * 1024
MAX_JSON_DEPTH = 64
METHOD_NOT_FOUND = -32601

SERVER_REQUESTS = frozenset({
    "client/registerCapability", "client/unregisterCapability",
    "window/workDoneProgress/create", "workspace/configuration",
})
SERVER_NOTIFICATIONS = frozenset({
    "$/progress", "pyright/beginProgress", "pyright/endProgress",
    "pyright/reportProgress", "textDocument/publishDiagnostics",
})


@dataclass(slots=True)
class PendingRequest:
    request_id: int
    method: str
    generation_nonce: str
    deadline: float
    completed: threading.Event
    result: object | None = None
    error: JsonRpcError | None = None
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class JsonRpcError:
    code: int
    message: str
    data: object | None
```

Expose `JsonRpcFrameReader.read()`, `encode_frame(message)`, `json_depth(value)`, and `LspProtocol.request(method, params, *, deadline, cancelled=None)`. One reader thread owns stdout. Pending keys are `(generation_nonce, request_id)`. On timeout or caller cancellation, mark pending cancelled, send `$/cancelRequest`, wait only inside the original deadline, and drop all later responses for that key. A response ID matching an active request twice is fatal. Any malformed/oversized message invokes one `fatal_callback(reason)`; process restart belongs to Task 6. Unknown server requests receive `MethodNotFound` without executing anything. Unknown server notifications are dropped with one bounded stable warning. `workspace/applyEdit`, `workspace/executeCommand`, `window/showDocument`, and all other mutation requests are never allowlisted.

- [ ] **Step 4: Run protocol tests and Ruff and verify GREEN**

Run: `uv run pytest tests/test_lsp_protocol.py -q`

Expected: PASS.

Run: `uv run ruff check scripts/lsp_protocol.py tests/test_lsp_protocol.py tests/fake_lsp_server.py`

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

```bash
git add scripts/lsp_protocol.py tests/test_lsp_protocol.py tests/fake_lsp_server.py
git commit -m "feat: add strict bounded LSP protocol"
```

### Task 5: Spawn Pyright With A Minimal Environment And Bounded Pending State

**Files:**
- Create: `scripts/lsp_process.py`
- Create: `tests/test_lsp_process.py`
- Modify: `tests/fake_lsp_server.py`

- [ ] **Step 1: Write failing environment, stderr, and owner tests**

```python
def test_server_environment_excludes_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "secret")
    monkeypatch.setenv("NPM_TOKEN", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    env = lsp_environment({"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": "C:\\Windows"})
    assert set(env) <= LSP_ENV_ALLOWLIST
    assert {"SSH_AUTH_SOCK", "NPM_TOKEN", "OPENAI_API_KEY"}.isdisjoint(env)


def test_stderr_ring_retains_only_last_four_mib(fake_server_command: list[str], tmp_path: Path) -> None:
    process = LspProcess.start(fake_server_command + ["--stderr-bytes", str(5 * 1024 * 1024)],
                               cwd=tmp_path, owner_root=tmp_path / "owner")
    process.wait_for_exit(deadline=time.monotonic() + 5)
    assert len(process.stderr_bytes()) == 4 * 1024 * 1024
```

Test `shell=False`, stdin/stdout/stderr pipes, process-generation nonce changes, owner-only scratch permissions where supported, cancellation directory under owner scratch, no inherited registry/cloud/agent credentials, and pending requests failed exactly once on process exit.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_lsp_process.py -q -k "environment or stderr or pending"`

Expected: FAIL with `ModuleNotFoundError: No module named 'lsp_process'`.

- [ ] **Step 3: Implement minimal environment and process state**

```python
MAX_STDERR_BYTES = 4 * 1024 * 1024
LSP_ENV_ALLOWLIST = frozenset({
    "COMSPEC", "HOME", "LANG", "LC_ALL", "PATH", "PATHEXT", "SYSTEMROOT",
    "TEMP", "TMP", "TMPDIR", "USERPROFILE", "WINDIR",
})


class ProcessState(str, Enum):
    PROCESS_RUNNING = "process_running"
    PROTOCOL_INITIALIZED = "protocol_initialized"
    WORKSPACE_READY = "workspace_ready"
    DEGRADED = "degraded"
    FAILED = "failed"


def lsp_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    values = os.environ if source is None else source
    return {name: values[name] for name in sorted(LSP_ENV_ALLOWLIST) if name in values}


@dataclass(slots=True)
class LspProcess:
    process: subprocess.Popen[bytes]
    protocol: LspProtocol
    owner_root: Path
    owner_nonce: str
    generation_nonce: str
    state: ProcessState
    started_monotonic: float
    last_used_monotonic: float
```

Expose exact methods `LspProcess.start(cls, command: Sequence[str], *, cwd: Path, owner_root: Path) -> LspProcess`, `LspProcess.request(self, method: str, params: object, *, deadline: float, cancelled: Callable[[], bool] | None = None) -> object`, and `LspProcess.stderr_bytes(self) -> bytes`. Use an in-memory `collections.deque[bytes]` ring trimmed by bytes, not lines. Write `owner.json` as restricted canonical JSON with owner PID, 32-hex owner nonce, generation nonce, start timestamp, command basename, and state; never persist arguments containing repository paths or environment values. Create only `cancellation/` and cleanup evidence beneath the approved owner root. Do not implement shutdown, restart, or process-tree killing in this task.

- [ ] **Step 4: Run process and protocol tests and verify GREEN**

Run: `uv run pytest tests/test_lsp_process.py tests/test_lsp_protocol.py -q -k "not lifecycle and not tree and not restart"`

Expected: PASS.

Run: `uv run ruff check scripts/lsp_process.py tests/test_lsp_process.py tests/fake_lsp_server.py`

Expected: PASS.

- [ ] **Step 5: Commit Task 5**

```bash
git add scripts/lsp_process.py tests/test_lsp_process.py tests/fake_lsp_server.py
git commit -m "feat: bound LSP process state"
```

### Task 6: Own The Full Process Lifecycle And Process Tree

**Files:**
- Create: `scripts/lsp_process_tree.py`
- Create: `tests/test_lsp_process_tree.py`
- Modify: `scripts/lsp_process.py`
- Modify: `tests/test_lsp_process.py`
- Modify: `tests/fake_lsp_server.py`

- [ ] **Step 1: Write failing shutdown, crash, timeout, and tree tests**

```python
@pytest.mark.parametrize("ending", ["shutdown", "crash", "timeout", "cancel"])
def test_no_descendant_survives_process_ending(ending: str, process_tree_server: ProcessTreeServer) -> None:
    process = process_tree_server.start()
    child_pid = process_tree_server.child_pid(process)
    process_tree_server.end(process, ending)
    assert wait_until(lambda: not pid_alive(process.process.pid), seconds=5)
    assert wait_until(lambda: not pid_alive(child_pid), seconds=5)


def test_protocol_failure_restarts_once_with_new_generation(process_tree_server: ProcessTreeServer) -> None:
    process = process_tree_server.start(scenario="fatal-once")
    first = process.generation_nonce
    assert process.request("test/echo", {}, deadline=time.monotonic() + 5) == {}
    assert process.generation_nonce != first
    assert process.restart_count == 1
```

Run real descendant tests on all supported OS families. Test graceful `shutdown` request then `exit`, a 2-second graceful cleanup budget inside the caller deadline, forced cleanup, idle timeout of 300 seconds, owner-process exit via `atexit`, reader-thread joins, second fatal failure without restart, and cleanup evidence after failure.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_lsp_process_tree.py tests/test_lsp_process.py -q -k "shutdown or descendant or restart or idle"`

Expected: FAIL because `lsp_process_tree` and lifecycle methods do not exist.

- [ ] **Step 3: Implement cross-platform ownership and one restart**

```python
@dataclass(slots=True)
class ProcessTree:
    process: subprocess.Popen[bytes]
    windows_job: int | None
    process_group: int | None
```

Expose exact methods `ProcessTree.spawn(cls, command: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> ProcessTree`, `ProcessTree.terminate(self, *, deadline: float) -> None`, and `ProcessTree.close(self) -> None`. On POSIX, use `start_new_session=True`, then `SIGTERM` and bounded `SIGKILL` against the process group. On Windows, create a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, assign the process before accepting requests, and terminate/close the Job Object during cleanup. If assignment fails, terminate the just-created process and return `failed`; never claim tree ownership. Do not use `preexec_fn`.

Extend `LspProcess` with `restart_count: int`, `shutdown(deadline)`, `cancel_all(reason)`, `restart(deadline)`, `close(deadline)`, and `idle_expired(now)`. Heartbeat `owner.json` atomically every 10 seconds with `heartbeat_at` and `expires_at` 30 seconds after the heartbeat; stop and join that thread during every close/failure branch. Fatal protocol/process failure allows exactly one fresh process generation. Pending keys from the prior generation are cancelled and all late responses are dropped. Normal close sends `shutdown`, waits for its response, sends `exit`, and then enforces tree cleanup. Failure writes `failure.json` with stable code, timestamp, PID, and generation nonce but no stderr or repository path; success removes the owner scratch after process exit.

- [ ] **Step 4: Run lifecycle tests and verify GREEN**

Run: `uv run pytest tests/test_lsp_process.py tests/test_lsp_process_tree.py tests/test_lsp_protocol.py -q`

Expected: PASS with zero surviving child PIDs.

Run: `uv run ruff check scripts/lsp_process.py scripts/lsp_process_tree.py tests/test_lsp_process.py tests/test_lsp_process_tree.py`

Expected: PASS.

- [ ] **Step 5: Commit Task 6**

```bash
git add scripts/lsp_process.py scripts/lsp_process_tree.py tests/test_lsp_process.py tests/test_lsp_process_tree.py tests/fake_lsp_server.py
git commit -m "feat: own LSP process tree lifecycle"
```

### Task 7: Enforce Repository Path Containment And Safe Logs

**Files:**
- Create: `scripts/lsp_security.py`
- Create: `tests/test_lsp_security.py`
- Modify: `scripts/lsp_positions.py`
- Modify: `tests/test_lsp_positions.py`

- [ ] **Step 1: Write failing request and response containment tests**

```python
def test_request_path_resolves_under_repository(repository: Path) -> None:
    scope = resolve_repository_scope(repository)
    source = resolve_repository_source(scope, "pkg/api.py")
    assert source.relative_path == "pkg/api.py"
    assert source.absolute_path == (repository / "pkg/api.py").resolve()


@pytest.mark.parametrize("path", ["../secret.py", "/tmp/secret.py", "C:/secret.py", "pkg/link.py"])
def test_escape_or_link_is_rejected(repository: Path, path: str) -> None:
    scope = resolve_repository_scope(repository)
    with pytest.raises(PathContainmentError):
        resolve_repository_source(scope, path)


def test_external_provider_location_is_filtered(repository: Path) -> None:
    scope = resolve_repository_scope(repository)
    result = normalize_provider_uri(scope, Path(sys.prefix, "lib", "typing.py").as_uri())
    assert result is None
```

Test POSIX symlinks, Windows junction/reparse points when available, case-fold collisions, encoded separators, NUL/control characters, file URI drive variants, UNC/network paths, nonexistent deletion paths, and redaction of home, repository, credentials, URLs with userinfo, and environment assignments.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_lsp_security.py tests/test_lsp_positions.py -q -k "containment or escape or external or redact"`

Expected: FAIL with `ModuleNotFoundError: No module named 'lsp_security'`.

- [ ] **Step 3: Implement the containment boundary**

```python
@dataclass(frozen=True, slots=True)
class RepositorySource:
    repository_id: str
    checkout_id: str
    relative_path: str
    absolute_path: Path
    uri: str


def redact_lsp_text(value: str, *, repository: RepositoryScope | None = None) -> str:
    redacted = _CREDENTIAL_ASSIGNMENT.sub("<redacted>", value)
    if repository is not None:
        redacted = redacted.replace(repository.checkout_root, "<repository>")
    return redacted[:1024]
```

Expose exact functions `resolve_repository_source(repository: RepositoryScope, relative_path: str, *, must_exist: bool = True) -> RepositorySource`, `normalize_provider_uri(repository: RepositoryScope, uri: str) -> RepositorySource | None`, and the shown `redact_lsp_text`. Define `_CREDENTIAL_ASSIGNMENT` as a bounded case-insensitive expression over `api_key|authorization|password|secret|token`. Require normalized relative POSIX paths. Resolve and compare with `RepositoryScope.checkout_root`; use `os.path.normcase` only in addition to, never instead of, resolved ancestry. Reject links/reparse points in every existing component. For a deleted target, hold the nearest existing parent under the repository and validate the final lexical component. Returned external URIs are counted as filtered provenance but never exposed as source locations. Extend `lsp_positions.file_uri_to_path()` only to return canonical local paths; containment remains exclusively in this module.

Document in module text and tests: the repository is trusted, the subprocess is not sandboxed, and containment validates navigation evidence rather than restricting every file Pyright can access.

- [ ] **Step 4: Run security tests and verify GREEN**

Run: `uv run pytest tests/test_lsp_security.py tests/test_lsp_positions.py tests/test_repository_scope.py -q`

Expected: PASS.

Run: `uv run ruff check scripts/lsp_security.py scripts/lsp_positions.py tests/test_lsp_security.py tests/test_lsp_positions.py`

Expected: PASS.

- [ ] **Step 5: Commit Task 7**

```bash
git add scripts/lsp_security.py scripts/lsp_positions.py tests/test_lsp_security.py tests/test_lsp_positions.py
git commit -m "feat: contain LSP repository paths"
```

### Task 8: Pin, Discover, Diagnose, And Explicitly Install Pyright 1.1.411

**Files:**
- Create: `scripts/pyright_profile.py`
- Create: `scripts/install_pyright.py`
- Create: `tests/test_pyright_profile.py`
- Create: `tests/test_install_pyright.py`
- Modify: `tests/code_kernel_helpers.py`

- [ ] **Step 1: Write failing profile precedence and installer tests**

```python
def test_discovery_prefers_matching_project_then_managed_then_system(
    repository: Path, state_root: Path, pyright_candidates: PyrightCandidates,
) -> None:
    result = discover_pyright(resolve_repository_scope(repository), state_root=state_root,
                              candidates=pyright_candidates)
    assert result.source == "project-local"
    assert result.version == "1.1.411"
    assert result.node_major == 22
    assert result.qualified is True


def test_query_discovery_never_downloads(monkeypatch: pytest.MonkeyPatch, repository: Path) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("network used"))
    assert discover_pyright(resolve_repository_scope(repository), state_root=repository).status == "missing"


def test_explicit_installer_verifies_digest_before_publish(tmp_path: Path, artifact: Path) -> None:
    result = install_pyright(state_root=tmp_path, artifact=artifact)
    assert result.root == tmp_path / "cache/code-tools/pyright/1.1.411"
    assert result.package_sha256 == PYRIGHT_PACKAGE_SHA256
    assert (result.root / "install-manifest.json").is_file()
```

Test version mismatch, package mismatch, Node missing, Node major mismatch, unsafe tar members, links/devices, duplicate members, oversized archive/member/count bounds, interrupted install, existing valid install idempotence, and no install from `discover_pyright()`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_pyright_profile.py tests/test_install_pyright.py -q`

Expected: FAIL because `pyright_profile` and `install_pyright` do not exist.

- [ ] **Step 3: Implement exact profile identity and precedence**

```python
PYRIGHT_VERSION = "1.1.411"
PYRIGHT_PACKAGE_URL = "https://registry.npmjs.org/pyright/-/pyright-1.1.411.tgz"
PYRIGHT_PACKAGE_SHA256 = "bd5c488fc20fa237a944279bf32cae2f986cf10d5d5d9e8705819859daeb2f4a"
PYRIGHT_PACKAGE_INTEGRITY = "sha512-03S/vmS5lF1S/tVbKc2WNXCMq8JWCwta/qIYjj1jvqbQhoy+N3NgBzHTSmUlbYD6DJwqQ5XHf108QujoqeURvw=="
QUALIFIED_NODE_MAJOR = 22
PYRIGHT_SERVER_RELATIVE = Path("package/langserver.index.js")


@dataclass(frozen=True, slots=True)
class PyrightIdentity:
    status: str
    source: str | None
    version: str | None
    node_executable: Path | None
    node_version: str | None
    node_major: int | None
    server_executable: Path | None
    executable_sha256: str | None
    package_sha256: str | None
    initialization_options_sha256: str
    configuration_sha256: str
    qualified: bool
    degradation_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PyrightCandidates:
    project_local: tuple[Path, ...]
    managed: tuple[Path, ...]
    system: tuple[Path, ...]
```

`discover_pyright(repository, *, state_root, candidates=None, deadline=None)` checks exactly: `<checkout>/node_modules/pyright/langserver.index.js`, approved managed root, then a system `pyright-langserver` package root. It runs `node --version` and reads package metadata under bounded deadlines. Qualification requires Pyright 1.1.411, exact npm integrity from a managed install manifest or project/system lockfile, Node major 22, exact initialization options, and exact configuration fingerprint. The managed receipt records the release artifact SHA-256 as `package_sha256` and the computed entrypoint SHA-256 as `executable_sha256`; discovery recomputes the entrypoint digest and requires equality with that receipt. A project-local installation is qualified only when its package-lock integrity equals the pinned npm SRI; a system installation without equivalent package provenance is degraded. Record full Node version while qualifying on major 22. Any mismatch is `status="degraded"`, not silently accepted.

Use these configuration values unless repository configuration provides an explicit value that is included in the fingerprint:

```python
PYRIGHT_CONFIGURATION = {
    "python": {"analysis": {
        "autoSearchPaths": True,
        "diagnosticMode": "openFilesOnly",
        "logLevel": "Error",
        "useLibraryCodeForTypes": True,
    }},
    "pyright": {
        "disableLanguageServices": False,
        "disableOrganizeImports": True,
        "disableTaggedHints": False,
    },
}
PYRIGHT_INITIALIZATION_OPTIONS = {"files": {"exclude": []}}
```

- [ ] **Step 4: Implement the explicit installer and verify GREEN**

`scripts/install_pyright.py` exposes `install_pyright(*, state_root: Path, artifact: Path | None = None, deadline: float | None = None) -> InstalledPyright` and CLI `uv run python scripts/install_pyright.py --state-root <absolute-path>`. Omitting `--artifact` is the only branch that downloads the exact pinned URL, and invoking this CLI is the operator's explicit action. Stream to a temporary file under `cache/code-tools/pyright/`, verify SHA-256 and npm SHA-512 integrity, manually extract regular files/directories under a temporary root without `TarFile.extractall`, verify `package/package.json` and `package/langserver.index.js`, write a canonical manifest, fsync, and atomically rename to `1.1.411`. Never call npm, never resolve `latest`, never install Node, and never invoke this module from query, MCP, doctor, or profile discovery.

Define `InstalledPyright` as frozen/slotted fields `root: Path`, `version: str`, `package_sha256: str`, `package_integrity: str`, `server_sha256: str`, and `manifest_sha256: str`.

Run: `uv run pytest tests/test_pyright_profile.py tests/test_install_pyright.py tests/test_code_kernel_helpers.py -q`

Expected: PASS.

Run: `uv run ruff check scripts/pyright_profile.py scripts/install_pyright.py tests/test_pyright_profile.py tests/test_install_pyright.py tests/code_kernel_helpers.py`

Expected: PASS.

- [ ] **Step 5: Commit Task 8**

```bash
git add scripts/pyright_profile.py scripts/install_pyright.py tests/test_pyright_profile.py tests/test_install_pyright.py tests/code_kernel_helpers.py
git commit -m "feat: pin explicit Pyright profile"
```

### Task 9: Implement Pyright Readiness, Document Sync, And Features

**Files:**
- Create: `scripts/pyright_session.py`
- Create: `tests/test_pyright_session.py`
- Modify: `scripts/lsp_process.py`
- Modify: `tests/fake_lsp_server.py`
- Modify: `tests/code_kernel_helpers.py`

- [ ] **Step 1: Write failing readiness and semantic feature tests**

```python
def test_query_ready_requires_config_open_and_document_symbol(
    pyright_session: PyrightSession, repository: Path,
) -> None:
    document = pyright_session.open_document("pkg/service.py", deadline=time.monotonic() + 60)
    assert pyright_session.readiness == "query_ready"
    assert document.version == 1
    assert pyright_session.readiness_evidence == (
        "initialize", "initialized", "configuration", "didOpen", "documentSymbol",
    )


def test_definition_references_hover_and_calls_are_provider_reported(
    pyright_session: PyrightSession,
) -> None:
    anchor = SourceAnchor("pkg/service.py", 10, 24, 0)
    assert pyright_session.definition(anchor, deadline=time.monotonic() + 10)
    assert pyright_session.references(anchor, deadline=time.monotonic() + 10).coverage == "provider_reported"
    assert pyright_session.hover(anchor, deadline=time.monotonic() + 10).contents
```

Add tests for missing/false capabilities, implementation support honesty, type definition, workspace/document symbols, push diagnostics, call hierarchy prepare/incoming/outgoing, no equivalence between references and calls, progress notifications, broken project degradation, startup timeout returning `not_ready`, four-process capacity, busy-capacity refusal, idle LRU eviction, and no complete negative before readiness.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_pyright_session.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'pyright_session'`.

- [ ] **Step 3: Implement readiness and document synchronization**

```python
STARTUP_SECONDS = 60.0
MAX_LSP_PROCESSES = 4


@dataclass(frozen=True, slots=True)
class OpenDocument:
    source: RepositorySource
    content: bytes
    source_sha256: str
    version: int


@dataclass(frozen=True, slots=True)
class ProviderLocations:
    locations: tuple[LspLocation, ...]
    coverage: str
    partial: bool


@dataclass(frozen=True, slots=True)
class LspLocation:
    uri: str
    range: LspRange


@dataclass(frozen=True, slots=True)
class ProviderHover:
    contents: str | None
    range: LspRange | None
    partial: bool


@dataclass(frozen=True, slots=True)
class ProviderCalls:
    direction: str
    locations: tuple[LspLocation, ...]
    coverage: str
    partial: bool


@dataclass(frozen=True, slots=True)
class ProviderDiagnostics:
    diagnostics: tuple[LspDiagnostic, ...]
    document_version: int | None
    partial: bool
```

Define `LspDiagnostic` as frozen/slotted fields `uri: str`, `range: LspRange`, `severity: int | None`, `code: str | None`, `message: str`, and `related: tuple[tuple[LspLocation, str | None], ...]`. Expose exact methods `PyrightSession.start(self, *, deadline: float) -> None`, `open_document(self, path: str, *, deadline: float) -> OpenDocument`, `synchronize(self, revision: WorkspaceRevision, *, deadline: float) -> WorkspaceDelta`, `definition|references|implementations|type_definition(self, anchor: SourceAnchor, *, deadline: float) -> ProviderLocations`, `hover(self, anchor: SourceAnchor, *, deadline: float) -> ProviderHover`, `incoming_calls|outgoing_calls(self, anchor: SourceAnchor, *, deadline: float) -> ProviderCalls`, `document_symbols(self, path: str, *, deadline: float) -> ProviderLocations`, `workspace_symbols(self, query: str, *, deadline: float) -> ProviderLocations`, and `diagnostics(self, path: str, *, deadline: float) -> ProviderDiagnostics`. Start with `node <server_executable> --stdio --cancellationReceive=file:<owner>/cancellation`. Send `initialize` with root URI/workspace folder, process ID, client info, `general.positionEncodings` ordered UTF-8, UTF-16, UTF-32, and only read feature capabilities. Parse server capabilities and default omitted position encoding to `PositionEncoding.UTF16`. Send `initialized` and `workspace/didChangeConfiguration`; answer `workspace/configuration` from the fingerprinted settings. Open full UTF-8 document text at version 1, then require a successful bounded `textDocument/documentSymbol` response for that file before `query_ready`. Initialization alone sets only `protocol_initialized`.

Define `PyrightSessionManager.get(repository: RepositoryScope, *, deadline: float) -> PyrightSession` and `close_all(*, deadline: float) -> None`. Key sessions by `checkout_id` and qualified profile identity, cap live processes at four, close least-recently-used idle sessions first, and return `not_ready` rather than exceed the cap when all four are active. Register `close_all` with the owning MCP process `atexit`; this is in-process lifecycle management, not a daemon.

- [ ] **Step 4: Implement create/edit/rename/delete sync and verify GREEN**

For changed open documents, send full-content `textDocument/didChange` and increment version. For create/delete/rename, send `workspace/didChangeWatchedFiles`; close deleted/old renamed URIs, then open a renamed target when queried. Re-read exact bytes after revision computation and fail stale if their hash differs. Store at most 10,000 latest diagnostics per URI in memory, keyed by document version when supplied; do not persist diagnostics or semantic results.

Run: `uv run pytest tests/test_pyright_session.py tests/test_lsp_process.py tests/test_lsp_protocol.py -q`

Expected: PASS.

Run: `uv run ruff check scripts/pyright_session.py scripts/lsp_process.py tests/test_pyright_session.py tests/fake_lsp_server.py`

Expected: PASS.

- [ ] **Step 5: Commit Task 9**

```bash
git add scripts/pyright_session.py scripts/lsp_process.py tests/test_pyright_session.py tests/fake_lsp_server.py tests/code_kernel_helpers.py
git commit -m "feat: add ready Pyright navigation session"
```

### Task 10: Build The Normalized Navigation Facade And Freshness Retry

**Files:**
- Create: `scripts/code_navigation.py`
- Create: `tests/test_code_navigation.py`
- Modify: `tests/code_kernel_helpers.py`

- [ ] **Step 1: Write failing normalized, ambiguity, fallback, and stale tests**

```python
def test_exact_definition_returns_complete_identity_and_provenance(
    navigation: CodeNavigation, repository: Path,
) -> None:
    scope = resolve_repository_scope(repository)
    result = navigation.query(NavigationRequest(
        scope, Capability.DEFINITIONS, "pkg/service.py", 10, 24,
    ), deadline=time.monotonic() + 60)
    assert result.status is NavigationStatus.OK
    assert result.provider == "pyright"
    assert result.provider_version == "1.1.411"
    assert result.workspace_revision_before == result.workspace_revision_after
    assert result.position_encoding in set(PositionEncoding)
    assert result.provenance[0].source == "lsp"


def test_second_revision_change_discards_both_answers_and_returns_stale(
    navigation: CodeNavigation, changing_repository: Path,
) -> None:
    result = navigation.query(definition_request(changing_repository),
                              deadline=time.monotonic() + 60)
    assert result.status is NavigationStatus.STALE
    assert result.locations == ()


def test_name_only_ambiguity_is_returned_not_chosen(navigation: CodeNavigation) -> None:
    result = navigation.resolve_symbol("execute", repository=navigation.repository)
    assert result.status is NavigationStatus.PARTIAL
    assert result.resolution is ResolutionLabel.AMBIGUOUS
```

Test all seven statuses, all eight resolution labels, exact field validation, unsupported implementations, missing Pyright structural fallback, failed Pyright fallback, provider results not graph-filtered, graph-only candidates appended after full LSP results, external-location filtering, one-edge verification, no deadness proof from empty LSP, and no semantic cache files or graph writes.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_code_navigation.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'code_navigation'`.

- [ ] **Step 3: Implement the shared immutable contracts and facade API**

Implement the `NavigationStatus`, `ResolutionLabel`, `NavigationRequest`, and `NavigationResult` declarations from “Shared Public Contracts”, plus:

```python
@dataclass(frozen=True, slots=True)
class Provenance:
    source: str
    provider: str
    version: str
    observation: str


@dataclass(frozen=True, slots=True)
class NavigationLocation:
    path: str
    range: PositionRange
    line: int
    character: int
    containing_symbol: str | None
    signature: str | None
    resolution: ResolutionLabel
    provenance: tuple[Provenance, ...]
```

Expose exact methods `CodeNavigation.query(self, request: NavigationRequest, *, deadline: float) -> NavigationResult`, `resolve_symbol(self, symbol: str, *, repository: RepositoryScope, deadline: float | None = None) -> NavigationResult`, `verify_edge(self, source: SourceAnchor, target: SourceAnchor, *, repository: RepositoryScope, deadline: float) -> NavigationResult`, and `close(self, *, deadline: float) -> None`. `NavigationRequest` validates `path`, one-based `line`, zero-based UTF-8 `character`, `offset >= 0`, `1 <= limit <= 100`, and `direction in {None,"incoming","outgoing"}`. Direction must be non-null exactly for `Capability.CALLS`. Capability routing is exact: definitions, references, implementations, type definitions/hover, diagnostics, and calls. Calls prefer call hierarchy when advertised; otherwise classify references using existing structural evidence and return `partial`. Name-only discovery calls existing exact symbol/Evidence Graph search, returns every declaration candidate, and requires caller disambiguation when more than one remains.

- [ ] **Step 4: Implement freshness and merge semantics and verify GREEN**

For each attempt: compute revision, synchronize, validate current source bytes and anchor, perform the full provider request, recompute revision, and compare. On first mismatch discard the response and retry once from a fresh pre-revision. On second mismatch return `stale` with no provider locations. Do not retain a cursor; `offset` reruns the request against a new current revision. The full provider-reported reference/implementation result is normalized before graph observations are merged. Sort and deduplicate by `(path, byte_start, byte_end, resolution, provider)` without using graph top-K as an LSP filter.

Use existing `Capability`: `DEFINITIONS`, `REFERENCES`, `IMPLEMENTATIONS`, `TYPE_DEFINITIONS`, `TYPES`, `CALLS`, and `DIAGNOSTICS`. Use existing `PositionRange` and `PositionEncoding`. Use `RepositoryScope.repository_id`, `checkout_id`, and `checkout_root`; do not introduce alternate repository identity.

Run: `uv run pytest tests/test_code_navigation.py tests/test_pyright_session.py tests/test_code_graph.py -q`

Expected: PASS.

Run: `uv run ruff check scripts/code_navigation.py tests/test_code_navigation.py tests/code_kernel_helpers.py`

Expected: PASS.

- [ ] **Step 5: Commit Task 10**

```bash
git add scripts/code_navigation.py tests/test_code_navigation.py tests/code_kernel_helpers.py
git commit -m "feat: normalize fresh code navigation"
```

### Task 11: Render Compact Deterministic Navigation Results

**Files:**
- Create: `scripts/code_navigation_renderer.py`
- Create: `tests/test_code_navigation_renderer.py`

- [ ] **Step 1: Write failing ordering, window, and token tests**

```python
def test_renderer_puts_status_identity_and_count_first() -> None:
    rendered = render_navigation(navigation_result(25), offset=0, limit=10)
    assert list(rendered)[:5] == ["status", "freshness", "provider", "symbol", "total"]
    assert len(rendered["groups"][0]["locations"]) <= 10
    assert rendered["truncated"] is True
    assert rendered["next_offset"] == 10


def test_default_output_is_bounded_without_silent_clipping() -> None:
    rendered = render_navigation(navigation_result(100), offset=0, limit=10)
    encoded = json.dumps(rendered, ensure_ascii=False, separators=(",", ":"))
    assert estimate_tokens(encoded) <= 1_200
    assert rendered["truncated"] is True
    assert rendered["omitted"] == 90
```

Test stable ordering across shuffled inputs, grouping by path and containing symbol, signatures without bodies, hover bound, diagnostics, offsets past total, limit 1..100, default limit 10, explicit source expansion absent by default, and byte-identical repeated rendering.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_code_navigation_renderer.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'code_navigation_renderer'`.

- [ ] **Step 3: Implement the renderer contract**

```python
DEFAULT_LIMIT = 10
MAX_LIMIT = 100
MAX_ESTIMATED_TOKENS = 1_200


def estimate_tokens(text: str) -> int:
    return (len(text.encode("utf-8")) + 3) // 4
```

Expose exact function `render_navigation(result: NavigationResult, *, offset: int | None = None, limit: int | None = None, include_source: bool = False) -> dict[str, object]`. Return keys in this exact order: `status`, `freshness`, `provider`, `symbol`, `total`, `requested_capability`, `effective_capability`, `position_encoding`, `readiness`, `repository`, `document_version`, `offset`, `limit`, `truncated`, `omitted`, `next_offset`, `resolution`, `groups`, `diagnostics`, `hover`, `provenance`, `warnings`. `freshness` contains `workspace_revision_before`, `workspace_revision_after`, and `current`. `provider` contains `name` and `version`. `repository` contains `repository_id` and `checkout_id`, never an absolute root.

Sort groups by repository-relative path then containing symbol; sort locations by byte range and resolution. Strip source bodies from signatures by retaining only the declaration line. `include_source=False` always for MCP Task 12; a future explicit expansion request requires a separate approved design. If output would exceed 1,200 estimated tokens, reduce the location window until it fits, preserve `total`, set `truncated`, `omitted`, and `next_offset`, and append `output_token_bound` to warnings. Never clip a string without adding `hover_truncated` or `signature_truncated` and the original UTF-8 byte count.

- [ ] **Step 4: Run renderer tests and verify GREEN**

Run: `uv run pytest tests/test_code_navigation_renderer.py tests/test_code_navigation.py -q`

Expected: PASS.

Run: `uv run ruff check scripts/code_navigation_renderer.py tests/test_code_navigation_renderer.py`

Expected: PASS.

- [ ] **Step 5: Commit Task 11**

```bash
git add scripts/code_navigation_renderer.py tests/test_code_navigation_renderer.py
git commit -m "feat: render compact navigation evidence"
```

### Task 12: Extend Existing `get_architecture` Modes And Deadlines Without Breaking Structural Calls

**Files:**
- Modify: `scripts/mcp_server.py:64,297-464,975-1078,1931-2093,2244-2256`
- Modify: `tests/test_mcp_server.py:19-247,1160-1245,1437-1539`

- [ ] **Step 1: Write failing closed-schema, mode, deadline, and compatibility tests**

```python
def test_get_architecture_has_precise_modes_without_a_thirteenth_tool() -> None:
    modes = mcp_server.TOOL_INPUT_SCHEMAS["get_architecture"]["properties"]["mode"]["enum"]
    assert modes == [
        "summary", "symbol", "callers", "callees", "dependencies", "path",
        "community", "impact", "definition", "references", "implementations",
        "type", "diagnostics",
    ]
    assert len(mcp_server.TOOL_INPUT_SCHEMAS) == 12


@pytest.mark.parametrize("mode", ["definition", "references", "implementations", "type", "diagnostics"])
def test_precise_modes_require_exact_position(mode: str) -> None:
    error = mcp_server._validate_tool_arguments(
        "get_architecture", {"directory": "C:/repo", "mode": mode, "path": "pkg/api.py"},
    )
    assert "line" in error and "character" in error


def test_callers_without_position_keeps_structural_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("code_graph.find_callers", lambda *a, **k: {"callers": []})
    data = run_tool("get_architecture", {"directory": ".", "mode": "callers", "symbol": "f"})
    assert data["mode"] == "callers"
    assert data.get("provider") is None
```

Test that precise callers/callees require all of `path,line,character` together, structural callers/callees still require `symbol`, exact modes reject `symbol` as a substitute, `directory` must resolve to the repository root rather than a child, `path` is relative, `offset >= 0`, `1 <= limit <= 100`, unknown fields fail, `live` remains structural-only, and all old valid calls retain their shapes.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_mcp_server.py -q -k "precise_modes or exact_position or structural_behavior or navigation_deadline"`

Expected: FAIL because precise modes and fields are absent.

- [ ] **Step 3: Add exact input branches and operation deadlines**

Add fields:

```python
"path": {"type": "string", "minLength": 1, "maxLength": 4096},
"line": {"type": "integer", "minimum": 1},
"character": {"type": "integer", "minimum": 0},
"offset": {"type": "integer", "minimum": 0, "default": 0},
"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
```

Keep `directory`, `mode`, `symbol`, `reverse`, `comparison`, `base`, `target`, `branch`, and `live`. Validation must use closed mode-specific branches even if the advertised MCP schema retains one top-level `properties` map for SDK compatibility. Exact modes are `definition`, `references`, `implementations`, `type`, and `diagnostics`. `callers` and `callees` select precise routing only when all three exact position fields are supplied; with none they retain current structural behavior; partial position triples fail validation.

```python
MCP_OPERATION_SECONDS = 10.0
MCP_LSP_STARTUP_SECONDS = 60.0
PRECISE_ARCHITECTURE_MODES = frozenset({
    "definition", "references", "implementations", "type", "diagnostics",
})


def _tool_operation_seconds(name: str, arguments: object) -> float:
    if name == "get_architecture" and isinstance(arguments, dict):
        mode = arguments.get("mode", "summary")
        positioned_calls = mode in {"callers", "callees"} and all(
            key in arguments for key in ("path", "line", "character")
        )
        if mode in PRECISE_ARCHITECTURE_MODES or positioned_calls:
            return MCP_LSP_STARTUP_SECONDS
    return MCP_OPERATION_SECONDS
```

Create the one absolute handler deadline before validation using this function in `_handle_tool_call()` and registered SDK calls. Existing modes remain 10 seconds. Precise modes get at most 60 seconds, including validation, discovery, startup, readiness, request, freshness retry, rendering, and envelope creation.

- [ ] **Step 4: Route through the facade and verify GREEN envelope semantics**

Add `_get_precise_architecture(directory, *, mode, path, line, character, offset=0, limit=10, deadline)` that requires `directory` equal the resolved `RepositoryScope.checkout_root`, maps modes to existing capabilities, calls one MCP-process-owned `CodeNavigation` manager, and returns `render_navigation()`. Map `type` to `Capability.TYPES`; that compound operation requests type definition plus hover without persistent reuse. Map positioned callers/callees to `Capability.CALLS` and `NavigationRequest.direction="incoming"|"outgoing"`.

The MCP `data` object exposes renderer fields plus `directory`, `mode`, and these exact normalized fields: `status`, `freshness`, `provider`, `symbol`, `total`, `requested_capability`, `effective_capability`, `position_encoding`, `readiness`, `repository`, `document_version`, `offset`, `limit`, `truncated`, `omitted`, `next_offset`, `resolution`, `groups`, `diagnostics`, `hover`, `provenance`, and `warnings`. The existing outer envelope remains unchanged. Set outer `partial=True` for normalized `partial|unsupported|not_ready|stale|timeout|error`; only `ok` is non-partial. Do not send raw LSP JSON or absolute external paths.

Run: `uv run pytest tests/test_mcp_server.py tests/test_code_navigation_renderer.py tests/test_code_navigation.py -q`

Expected: PASS, including exactly 12 tools and all previous structural-mode tests.

Run: `uv run ruff check scripts/mcp_server.py tests/test_mcp_server.py`

Expected: PASS.

- [ ] **Step 5: Commit Task 12**

```bash
git add scripts/mcp_server.py tests/test_mcp_server.py
git commit -m "feat: expose precise navigation modes"
```

### Task 13: Diagnose Pyright And Protect Live Or Retained LSP State

**Files:**
- Modify: `scripts/doctor.py:40-120,1296-1330,3418-3768`
- Modify: `tests/test_doctor.py`
- Modify: `tests/test_runtime_deletion_contract.py`

- [ ] **Step 1: Write failing health and deletion tests**

```python
def test_doctor_reports_missing_and_mismatched_pyright(tmp_path: Path, monkeypatch) -> None:
    missing = doctor._pyright_check(tmp_path, tmp_path, deadline=float("inf"))
    assert missing["status"] == "degraded"
    assert missing["details"]["codes"] == ["pyright_missing"]
    monkeypatch.setattr(doctor, "discover_pyright", lambda *a, **k: mismatched_identity())
    mismatch = doctor._pyright_check(tmp_path, tmp_path, deadline=float("inf"))
    assert mismatch["status"] == "degraded"
    assert "pyright_version_mismatch" in mismatch["details"]["codes"]


def test_live_owner_and_retained_failure_block_run_deletion(tmp_path: Path, monkeypatch) -> None:
    create_lsp_owner(tmp_path, live=True)
    create_lsp_failure(tmp_path, age_days=1)
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: True)
    check = doctor._lsp_runtime_check(tmp_path, datetime.now(timezone.utc))
    deletion = doctor._run_deletion_check(tmp_path, datetime.now(timezone.utc), collected={"lsp": check})
    assert {item["code"] for item in deletion["blockers"]} >= {
        "lsp_owner_live", "lsp_failure_evidence_retained",
    }
```

Test valid qualified profile, package/executable/Node/config mismatch, unsafe managed root, malformed owner records, dead owner without failure evidence, failure evidence at 7 days minus one second and at 7 days, scan truncation, deadline, and doctor never installing or downloading.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_doctor.py tests/test_runtime_deletion_contract.py -q -k "pyright or lsp_owner or lsp_failure"`

Expected: FAIL because `_pyright_check` and `_lsp_runtime_check` do not exist.

- [ ] **Step 3: Implement bounded doctor checks**

```python
LSP_FAILURE_RETENTION = timedelta(days=7)
MAX_LSP_OWNER_ROWS = 128
```

Expose exact functions `_pyright_check(root: Path, state_root: Path, *, deadline: float) -> dict` and `_lsp_runtime_check(state_root: Path, now: datetime, *, deadline: float = float("inf")) -> dict`. `_pyright_check` reports source, version, package SHA-256, executable SHA-256, full Node version, Node major, initialization fingerprint, configuration fingerprint, `qualified`, and stable codes without absolute executable paths. Missing is degraded with an explicit installer command in `recommended_action`; mismatch is degraded and is never qualified. The check calls discovery only and has no network or mutation path.

`_lsp_runtime_check` safely scans only `run/lsp/<32-hex>/owner.json|failure.json`. A live PID plus matching unexpired heartbeat is `lsp_owner_live`. A failure younger than seven days is `lsp_failure_evidence_retained`. A dead or expired owner record without `failure.json` is treated as crash evidence from its last heartbeat and retained under the same seven-day bound. At age greater than or equal to seven days, failure/crash evidence is reported as expired and does not block whole-`run/` deletion. An unreadable, unsafe, malformed, or truncated tree adds `lsp_state_unreadable` and fails closed. Doctor repair does not delete these records.

- [ ] **Step 4: Add deletion aggregation and verify GREEN**

Add both checks to `run_doctor()`. Add `lsp` to `_run_deletion_check()` aggregation beside transactions, queue, and archives. Health may be degraded while deletion remains blocked; do not turn retained failure evidence into an error. `doctor --repair`, `install.sh`, and `install.ps1` never remove `run/lsp` and never install Pyright.

Run: `uv run pytest tests/test_doctor.py tests/test_runtime_deletion_contract.py tests/test_mcp_server.py -q`

Expected: PASS.

Run: `uv run ruff check scripts/doctor.py tests/test_doctor.py tests/test_runtime_deletion_contract.py`

Expected: PASS.

- [ ] **Step 5: Commit Task 13**

```bash
git add scripts/doctor.py tests/test_doctor.py tests/test_runtime_deletion_contract.py
git commit -m "feat: diagnose and protect LSP runtime"
```

### Task 14: Add Deterministic Qualification Fixtures And Benchmark Gates

**Files:**
- Create: `benchmark/code-navigation-python-v1.json`
- Create: `benchmark/code-navigation-python-v1.schema.json`
- Create: `benchmark/run_code_navigation.py`
- Create: `benchmark/generate_python_qualification.py`
- Create: `tests/test_code_navigation_benchmark.py`
- Modify: `benchmark/COMPARATIVE.md`

- [ ] **Step 1: Write failing manifest, determinism, and gate tests**

```python
def test_qualification_generator_is_exactly_100_000_lines(tmp_path: Path) -> None:
    first = generate_qualification_repository(tmp_path / "first")
    second = generate_qualification_repository(tmp_path / "second")
    assert first.line_count == second.line_count == 100_000
    assert first.source_manifest_sha256 == second.source_manifest_sha256


def test_acceptance_gate_requires_every_production_threshold() -> None:
    report = passing_report()
    assert evaluate_gates(report)["passed"] is True
    for field in (
        "definition_accuracy", "reference_f1", "stale_answer_count",
        "orphan_process_count", "recovery_rate", "default_items",
        "default_estimated_tokens", "warm_overhead_p95_ms", "cold_readiness_seconds",
        "client_rss_mib",
    ):
        assert evaluate_gates(degrade(report, field))["passed"] is False
```

Test closed schemas, exact pinned manifest identity, gold-query reproducibility, precision/recall/F1 arithmetic, p50/p95 selection, token categories, private corpus exclusion from checked-in reports, and explicit `market_superiority_claimed: false`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_code_navigation_benchmark.py -q`

Expected: FAIL because the benchmark runner and manifest do not exist.

- [ ] **Step 3: Implement exact manifest and metrics**

The closed manifest contains:

```json
{
  "schema_version": "code-navigation-python/v1",
  "fixture_seed": 411,
  "fixture_lines": 100000,
  "python_min": "3.10",
  "pyright_version": "1.1.411",
  "pyright_package_sha256": "bd5c488fc20fa237a944279bf32cae2f986cf10d5d5d9e8705819859daeb2f4a",
  "node_major": 22,
  "definition_queries": 200,
  "reference_queries": 100,
  "call_queries": 100,
  "edit_rename_delete_cycles": 50,
  "crash_cycles": 20,
  "default_limit": 10,
  "max_estimated_tokens": 1200,
  "market_superiority_claimed": false
}
```

`generate_python_qualification.py` emits deterministic modules, inheritance, protocols, imports, calls, Unicode identifiers, ambiguous names, broken files, and exact gold JSON while totaling exactly 100,000 physical source lines. It does not invoke Git, Pyright, package installation, or the network. The runner initializes Git deterministically after generation and records the resulting commit.

`run_code_navigation.py` records definition exact-location accuracy; reference/call/impact precision, recall, and F1 where gold exists; task success; citation correctness; uncached input, cache-read, raw tool, and output tokens per solved task; cold readiness; warm p50/p95; edit-to-fresh latency; peak LLM Wiki RSS excluding the Pyright PID tree; errors; recovery; stale-result rate; and orphan-process rate. Because this slice has no semantic result cache, `cache_read_tokens` is always zero and the report labels it `not_applicable_no_result_cache`.

- [ ] **Step 4: Enforce all acceptance gates and verify GREEN**

`evaluate_gates()` passes only when:

- exact definitions are at least 99%;
- reference F1 is at least 95%;
- stale fixture answers are exactly zero;
- orphan processes are exactly zero after normal shutdown, crash, timeout, and cancellation;
- bounded crash recovery is 20 of 20, or 100%;
- default output has at most 10 items and at most 1,200 estimated tokens;
- on the fixed 100 KLOC repository, warm LLM Wiki overhead over direct warmed Pyright is at most 20 ms at p95;
- cold query readiness is at most 60 seconds;
- LLM Wiki process peak RSS excluding Pyright is below 100 MiB.

The benchmark report pins generated source-manifest SHA-256, Git commit, Python full version, Pyright version and package digest, Node full version and major, OS/version/architecture, CPU model/core count, RAM class, and gold-query SHA-256. `--operator-corpus <absolute-path>` adds a private local result section and never writes source paths, source text, or private gold data into tracked output. It supplements rather than replaces the public qualification repository.

Run: `uv run pytest tests/test_code_navigation_benchmark.py -q`

Expected: PASS.

Run: `uv run python benchmark/run_code_navigation.py --fixture --correctness-only --require-gates`

Expected: PASS with definition accuracy at least 0.99, reference F1 at least 0.95, zero stale answers, zero orphan processes, and recovery rate 1.0.

- [ ] **Step 5: Commit Task 14**

```bash
git add benchmark/code-navigation-python-v1.json benchmark/code-navigation-python-v1.schema.json benchmark/run_code_navigation.py benchmark/generate_python_qualification.py benchmark/COMPARATIVE.md tests/test_code_navigation_benchmark.py
git commit -m "test: qualify Python code navigation"
```

### Task 15: Finish Cross-Platform CI, Synchronized Docs, And Full Verification

**Files:**
- Modify: `.github/workflows/tests.yml:33-88`
- Modify: `README.md`
- Modify: `README.ru.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/CODE-NAVIGATION.md`
- Modify: `docs/USER-GUIDE.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/STRUCTURE.md`
- Modify: `docs/operating-model.md`
- Modify: `tests/README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `CHANGELOG.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `tests/test_readme_i18n.py`
- Modify: `tests/test_structure.py`

- [ ] **Step 1: Write failing CI and documentation contract tests**

```python
def test_ci_qualifies_real_pyright_on_all_supported_os_families() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["pyright-navigation"]
    assert job["strategy"]["matrix"]["os"] == ["ubuntu-latest", "windows-latest", "macos-latest"]
    assert job["strategy"]["matrix"]["python"] == ["3.10"]
    assert job["strategy"]["matrix"]["node"] == ["22.23.1"]


def test_docs_state_security_install_and_market_truth() -> None:
    text = (ROOT / "docs/CODE-NAVIGATION.md").read_text(encoding="utf-8")
    for value in (
        "trusted local repositories", "not an OS sandbox", "Pyright 1.1.411",
        "cache/code-tools/pyright/1.1.411/", "run/lsp/<owner-nonce>/",
        "never downloads during a query", "market superiority remains unclaimed",
    ):
        assert value in text
```

Add i18n assertions that all three READMEs expose the same explicit install command, modes, Pyright version, and trust limitation. Replace brittle suite-count marketing in all three READMEs with synchronized “full regression suite” wording; keep historical counts in prior changelog entries unchanged.

- [ ] **Step 2: Run focused docs tests and verify RED**

Run: `uv run pytest tests/test_readme_i18n.py tests/test_structure.py -q -k "pyright or code_navigation or real_pyright"`

Expected: FAIL because final docs and the `pyright-navigation` CI job are absent.

- [ ] **Step 3: Add cross-platform real-Pyright CI**

Add one `pyright-navigation` job with `fail-fast: false`, OS matrix `[ubuntu-latest, windows-latest, macos-latest]`, Python `3.10`, Node `22.23.1`, and a 15-minute timeout. Steps are checkout, setup uv, install/pin Python, `uv sync --locked --dev`, setup Node, explicit `uv run python scripts/install_pyright.py --state-root "$RUNNER_TEMP/llm-wiki-state"`, focused protocol/session/process-tree/security tests, and `benchmark/run_code_navigation.py --fixture --correctness-only --require-gates`. Set `LLM_WIKI_STATE_ROOT` to a runner-local temp directory. The normal six-job matrix continues running protocol/unit tests without requiring Pyright.

Add a Linux Python 3.10 qualification step for the 100 KLOC latency/RSS gate:

```yaml
- name: Fixed 100 KLOC Python qualification gate
  if: matrix.os == 'ubuntu-latest'
  run: uv run python benchmark/run_code_navigation.py --fixture --qualification --require-gates
```

The job invokes only the explicit installer. No test or query performs a download. Process-tree and real Pyright tests run on Windows, Linux, and macOS before release.

- [ ] **Step 4: Write synchronized operator documentation**

`docs/CODE-NAVIGATION.md` documents exact installation:

```bash
uv run python scripts/install_pyright.py --state-root "$LLM_WIKI_STATE_ROOT"
```

It documents all `get_architecture` modes and fields, one-based line plus zero-based UTF-8 byte character, status semantics, 60-second precise deadline, stateless offsets, structural fallback, readiness limits, no complete-negative promise, create/edit/rename/delete synchronization, no semantic cache, no graph publication, exact protocol bounds, Node major 22 qualification, trusted-repository requirement, no sandbox claim, no hidden download/update, doctor codes, seven-day failure-evidence retention, and the explicit installer. It states that Pyright can read configured external environments/stubs/libraries and these become fingerprinted provenance. It states that market superiority remains unclaimed.

Update `docs/STRUCTURE.md` from approved target to implemented Python slice only after all focused tests pass. Keep other languages as unimplemented future candidates. Keep `AGENTS.md` and `CLAUDE.md` byte-identical. Add an `Unreleased` changelog entry without a release version bump. Synchronize `README.md`, `README.ru.md`, and `README.zh-CN.md` in the same change. Do not modify `pyproject.toml` version because this task is not a release.

- [ ] **Step 5: Run complete verification and verify GREEN**

Run: `uv run pytest tests/test_lsp_paths.py tests/test_lsp_positions.py tests/test_workspace_revision.py tests/test_lsp_protocol.py tests/test_lsp_process.py tests/test_lsp_process_tree.py tests/test_lsp_security.py tests/test_pyright_profile.py tests/test_install_pyright.py tests/test_pyright_session.py tests/test_code_navigation.py tests/test_code_navigation_renderer.py tests/test_code_navigation_benchmark.py tests/test_mcp_server.py tests/test_doctor.py tests/test_runtime_deletion_contract.py tests/test_structure.py tests/test_readme_i18n.py -q`

Expected: PASS.

Run: `uv run python scripts/lint_memory.py --scope all --fail-on-findings --allowed-categories orphan_daily_logs missing_backlinks missing_sources_section temporal_validity stale_compiled`

Expected: PASS with no new blocking finding.

Run: `uv run ruff check scripts/ tests/ benchmark/run_code_navigation.py benchmark/generate_python_qualification.py`

Expected: PASS.

Run: `uv run pytest tests/test_readme_i18n.py -q`

Expected: PASS.

Run: `uv run pytest -q`

Expected: PASS with zero failed tests.

Run: `uv run python benchmark/run_code_navigation.py --fixture --qualification --require-gates`

Expected: PASS all thresholds: definitions at least 99%, reference F1 at least 95%, zero stale answers, zero orphan processes, 100% bounded recovery, at most 10 default items, at most 1,200 estimated tokens, warm overhead p95 at most 20 ms, cold readiness at most 60 seconds, and client RSS below 100 MiB excluding Pyright.

- [ ] **Step 6: Commit Task 15**

```bash
git add .github/workflows/tests.yml README.md README.ru.md README.zh-CN.md docs/CODE-NAVIGATION.md docs/USER-GUIDE.md docs/ARCHITECTURE.md docs/STRUCTURE.md docs/operating-model.md tests/README.md CONTRIBUTING.md CHANGELOG.md AGENTS.md CLAUDE.md tests/test_readme_i18n.py tests/test_structure.py
git commit -m "docs: ship qualified Python navigation"
```

## Final Acceptance Checklist

- [ ] Exactly 12 MCP tools remain; only `get_architecture` gained modes.
- [ ] Existing `summary`, `symbol`, structural `callers`, structural `callees`, `dependencies`, `path`, `community`, and `impact` calls retain their prior behavior and 10-second deadline.
- [ ] Exact `definition`, `references`, `implementations`, `type`, `diagnostics`, and positioned `callers|callees` use the 60-second precise deadline and exact position contract.
- [ ] Input positions are one-based lines and zero-based UTF-8 byte offsets; the facade validates current bytes and converts to negotiated `PositionEncoding`.
- [ ] Every result has provider/version, repository/checkout, requested/effective capability, encoding, ranges, readiness, pre/post revision, document version, resolution, and provenance.
- [ ] The seven normalized statuses are exactly `ok|partial|unsupported|not_ready|stale|timeout|error` with the semantics in this plan.
- [ ] Protocol bounds are exactly 8 MiB frame, 32 pending requests, 10,000 locations, 10,000 diagnostics, 256 KiB hover, 4 MiB stderr, and JSON depth 64.
- [ ] Fatal protocol failures terminate the instance and allow at most one restart; cancelled/restarted late responses are dropped by generation nonce.
- [ ] Startup readiness requires initialize, initialized, configuration, didOpen, and a successful target-file document-symbol probe; initialize alone is not readiness.
- [ ] Freshness recomputes the workspace revision after each response, retries one mismatch once, and returns `stale` after a second mismatch.
- [ ] Create, edit, rename, and delete tests return no stale answer.
- [ ] Shutdown, crash, timeout, and cancellation leave no orphan process or descendant on Windows, Linux, or macOS.
- [ ] Pyright discovery order is matching project-local, managed, then system; any version/digest/Node/config mismatch is degraded.
- [ ] Query, MCP, doctor, and discovery code contain no download or update call; only `scripts/install_pyright.py` can fetch the pinned artifact after explicit invocation.
- [ ] Managed files live only under `cache/code-tools/pyright/1.1.411/`; owner scratch lives only under `run/lsp/<owner-nonce>/`.
- [ ] The environment allowlist excludes agent, cloud, SSH, package-registry, and API credentials.
- [ ] Documentation says trusted repository and not an OS sandbox; it does not imply Pyright is unable to write or read other user-accessible paths.
- [ ] Raw LSP JSON, absolute repository roots, external locations, stderr, and secrets are absent from MCP results and retained failure evidence.
- [ ] No Serena/multilspy runtime, Rust, interactive SCIP dependency, new graph, new MCP tool, new root, persistent daemon, semantic result cache, or graph publication exists.
- [ ] Context Compiler remains only on broad synthesis paths.
- [ ] All correctness, reliability, latency, token, RSS, and cross-platform gates pass.
- [ ] Public documentation makes no market superiority claim.

## Plan Self-Review Commands

Run after saving this plan and before implementation handoff:

```powershell
$forbidden = @('T' + 'BD', 'T' + 'ODO', 'implement ' + 'later', 'fill in ' + 'details', 'similar ' + 'to', 'add ' + 'appropriate', 'handle edge ' + 'cases'); git grep -n -i -E ($forbidden -join '|') -- docs/superpowers/plans/2026-07-22-python-pyright-navigation.md
```

Expected: no matches.

```powershell
git grep -n -E 'NavigationStatus|NavigationRequest|NavigationResult|PyrightIdentity|WorkspaceRevision|PositionEncoding|Capability|RepositoryScope' -- docs/superpowers/plans/2026-07-22-python-pyright-navigation.md
```

Expected: every later use matches the declarations and existing imported type names exactly.

```powershell
git grep -n -E '8 MiB|32 outstanding|10,000 normalized|10,000 diagnostics|256 KiB|4 MiB|depth 64|60 seconds|99%|95%|20 ms|100 MiB|1,200' -- docs/superpowers/plans/2026-07-22-python-pyright-navigation.md
```

Expected: all protocol and acceptance thresholds are present.
