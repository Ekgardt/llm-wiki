"""Code navigation fault paths the facade tests leave unexercised.

Each case names one failure the facade must turn into a result, not a crash: a
revision that times out or fails before or after the provider answers, a
workspace that moves under both attempts, a target that cannot be read, an
anchor or verifier that fails, a type query whose two provider calls fail in
different ways, and a source-document cache that must replace and evict.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import replace
from pathlib import Path

import code_navigation
import pytest
from code_intelligence import Capability, PositionEncoding, PositionRange
from code_navigation import (
    CodeNavigation,
    NavigationLocation,
    NavigationRequest,
    Provenance,
    ResolutionLabel,
)
from lsp_positions import LspPosition, LspRange, SourceAnchor, SourceDocument
from lsp_security import resolve_repository_source
from pyright_session import (
    LspLocation,
    OpenDocument,
    ProviderCalls,
    ProviderHover,
    ProviderLocations,
    PyrightSession,
)
from repository_scope import RepositoryScope, resolve_repository_scope
from workspace_revision import WorkspaceRevision

from tests.code_kernel_helpers import (
    SemanticPyrightFixture,
    create_python_repository,
    create_semantic_pyright_fixture,
)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return create_python_repository(tmp_path / "repository")


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    from reliable_memory import validate_state_root

    root = tmp_path / "state"
    validate_state_root(root)
    return root


@pytest.fixture
def semantic_pyright(repository: Path) -> SemanticPyrightFixture:
    return create_semantic_pyright_fixture(repository)


@pytest.fixture
def navigation(repository: Path, state_root: Path, semantic_pyright: SemanticPyrightFixture):
    scope = resolve_repository_scope(repository)
    session = PyrightSession(scope, semantic_pyright.identity, state_root=state_root)
    candidates = (_graph_location("pkg/api.py", 6, 15),)
    facade = CodeNavigation(
        scope,
        session,
        semantic_pyright.identity,
        structural_candidates=lambda request, deadline: candidates,
        symbol_resolver=lambda symbol, repo, deadline: candidates,
        edge_verifier=lambda source, target, repo, deadline: True,
    )
    try:
        yield facade, session, scope
    finally:
        session.close(deadline=time.monotonic() + 5)


def _graph_location(path: str, start: int, end: int) -> NavigationLocation:
    return NavigationLocation(
        path,
        PositionRange(start, end),
        1,
        0,
        None,
        None,
        ResolutionLabel.GRAPH_CANDIDATE,
        (Provenance("graph", "evidence-graph", "structural", "name_resolution"),),
    )


def _anchor(scope: RepositoryScope, path: str, line: int, character: int) -> SourceAnchor:
    content = resolve_repository_source(scope, path).absolute_path.read_bytes()
    return SourceDocument.from_bytes(path, content).validate_anchor(line=line, character=character)


def _raise(error: BaseException):
    def call(*args, **kwargs):
        raise error

    return call


def _pin_attempt(
    monkeypatch: pytest.MonkeyPatch, session: PyrightSession, scope: RepositoryScope
) -> WorkspaceRevision:
    """One fixed workspace revision, a synchronized provider and an open document."""
    session._position_encoding = PositionEncoding.UTF8
    revision = code_navigation._compute_revision(scope, deadline=time.monotonic() + 5)
    revision = replace(revision, revision_sha256="a" * 64)
    source = resolve_repository_source(scope, "pkg/service.py")
    content = source.absolute_path.read_bytes()
    document = OpenDocument(source, content, hashlib.sha256(content).hexdigest(), 1)
    monkeypatch.setattr(code_navigation, "_compute_revision", lambda repository, *, deadline: revision)
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(session, "open_document", lambda path, *, deadline: document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations((), "provider_reported", False),
    )
    return revision


def _moving_post_revision(revision: WorkspaceRevision):
    """Every post-request revision differs from the one before it."""
    calls = {"count": 0}

    def post_revision(repository, expected, *, deadline):
        calls["count"] += 1
        return replace(revision, revision_sha256=str(calls["count"] % 10) * 64)

    return post_revision


def _operation(name: str, facade: CodeNavigation, scope: RepositoryScope):
    deadline = time.monotonic() + 5
    operations = {
        "symbol": lambda: facade.resolve_symbol("execute", repository=scope, deadline=deadline),
        "edge": lambda: facade.verify_edge(
            _anchor(scope, "pkg/service.py", 10, 20),
            _anchor(scope, "pkg/api.py", 1, 4),
            repository=scope,
            deadline=deadline,
        ),
        "query": lambda: facade.query(
            NavigationRequest(scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20),
            deadline=deadline,
        ),
    }
    return operations[name]()


REVISION_FAULTS = {
    "timeout": ("_compute_revision", _raise(TimeoutError("revision"))),
    "oserror": ("_compute_revision", _raise(OSError("revision"))),
    "post-oserror": ("_compute_post_revision", _raise(OSError("post"))),
    "post-timeout": ("_compute_post_revision", _raise(TimeoutError("post"))),
    "read-fails": ("read_stable_bytes", _raise(OSError("gone"))),
}

EXPECTED_REVISION_FAULTS = {
    ("symbol", "timeout"): ("timeout", "symbol revision computation timed out"),
    ("symbol", "oserror"): ("error", "symbol revision computation failed"),
    ("symbol", "empty"): ("error", "symbol revision is unavailable"),
    ("symbol", "post-oserror"): ("error", "post-resolution revision failed"),
    ("symbol", "post-timeout"): ("timeout", "post-resolution revision timed out"),
    ("symbol", "post-moves"): ("stale", "workspace changed across symbol resolution"),
    ("symbol", "read-fails"): ("stale", "symbol target changed during resolution"),
    ("edge", "timeout"): ("timeout", "edge revision computation timed out"),
    ("edge", "oserror"): ("error", "edge revision computation failed"),
    ("edge", "empty"): ("error", "edge revision is unavailable"),
    ("edge", "post-oserror"): ("error", "post-edge revision failed"),
    ("edge", "post-timeout"): ("timeout", "post-edge revision timed out"),
    ("edge", "post-moves"): ("stale", "workspace changed across edge verification"),
    ("edge", "read-fails"): ("stale", "edge target changed during verification"),
    ("query", "timeout"): ("timeout", "revision computation timed out"),
    ("query", "oserror"): ("error", "revision computation failed"),
    ("query", "empty"): ("error", "workspace revision is unavailable"),
    ("query", "post-oserror"): ("error", "post-request revision failed"),
    ("query", "post-timeout"): ("timeout", "post-request revision timed out"),
    ("query", "post-moves"): ("stale", "workspace changed across the request"),
    ("query", "read-fails"): ("stale", "navigation target changed during the request"),
}


def _install_fault(
    monkeypatch: pytest.MonkeyPatch, fault: str, revision: WorkspaceRevision
) -> None:
    faults = {
        **REVISION_FAULTS,
        "empty": (
            "_compute_revision",
            lambda repository, *, deadline: replace(revision, revision_sha256=""),
        ),
        "post-moves": ("_compute_post_revision", _moving_post_revision(revision)),
    }
    name, value = faults[fault]
    monkeypatch.setattr(code_navigation, name, value)


@pytest.mark.parametrize(("operation", "fault"), sorted(EXPECTED_REVISION_FAULTS))
def test_revision_fault_becomes_a_result_without_facts(
    navigation, monkeypatch: pytest.MonkeyPatch, operation: str, fault: str
) -> None:
    facade, session, scope = navigation
    revision = _pin_attempt(monkeypatch, session, scope)
    _install_fault(monkeypatch, fault, revision)

    result = _operation(operation, facade, scope)

    status, warning = EXPECTED_REVISION_FAULTS[(operation, fault)]
    assert (result.status.value, result.warnings) == (status, (warning,))
    assert result.locations == ()


EDGE_FAULTS = {
    "anchor-timeout": ("timeout", "edge anchor validation timed out"),
    "anchor-moves": ("stale", "edge target changed during verification"),
    "verifier-timeout": ("timeout", "edge verifier timed out"),
    "verifier-error": ("error", "edge verifier failed"),
    "verifier-false": ("partial", "no structural edge proof"),
    "verifier-not-boolean": ("error", "edge verifier failed"),
}


def _edge_patches(fault: str) -> dict[str, object]:
    anchors = {
        "anchor-timeout": _raise(TimeoutError("anchor")),
        "anchor-moves": _raise(code_navigation._RevisionMismatch()),
    }
    verifiers = {
        "verifier-timeout": _raise(TimeoutError("verifier")),
        "verifier-error": _raise(RuntimeError("verifier")),
        "verifier-false": lambda *args: False,
        "verifier-not-boolean": lambda *args: 1,
    }
    return {"_validate_revision_anchor": anchors.get(fault), "_edge_verifier": verifiers.get(fault)}


@pytest.mark.parametrize("fault", sorted(EDGE_FAULTS))
def test_edge_verification_faults_name_their_cause(
    navigation, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    facade, session, scope = navigation
    _pin_attempt(monkeypatch, session, scope)
    patches = _edge_patches(fault)
    anchor_fault = patches["_validate_revision_anchor"]
    monkeypatch.setattr(
        code_navigation,
        "_validate_revision_anchor",
        anchor_fault or code_navigation._validate_revision_anchor,
    )
    monkeypatch.setattr(facade, "_edge_verifier", patches["_edge_verifier"] or facade._edge_verifier)

    result = _operation("edge", facade, scope)

    status, warning = EDGE_FAULTS[fault]
    assert (result.status.value, result.warnings) == (status, (warning,))


def _resolver_failing_midway(symbol, repository, deadline):
    yield _graph_location("pkg/api.py", 6, 15)
    raise RuntimeError("resolver")


SYMBOL_RESOLVERS = {
    "fails-midway": (_resolver_failing_midway, ("error", ("symbol resolver failed",))),
    "times-out": (_raise(TimeoutError("resolver")), ("timeout", ("symbol resolver timed out",))),
    "finds-nothing": (lambda *args: (), ("partial", ("no structural candidates",))),
}


@pytest.mark.parametrize("mode", sorted(SYMBOL_RESOLVERS))
def test_symbol_resolver_failure_keeps_nothing_it_did_not_bound(
    navigation, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    facade, session, scope = navigation
    _pin_attempt(monkeypatch, session, scope)
    resolver, expected = SYMBOL_RESOLVERS[mode]
    monkeypatch.setattr(facade, "_symbol_resolver", resolver)

    result = _operation("symbol", facade, scope)

    assert (result.status.value, result.warnings) == expected
    assert result.locations == ()


def test_symbol_resolution_rejects_an_infinite_deadline(navigation) -> None:
    facade, _session, scope = navigation

    with pytest.raises(ValueError, match="deadline must be finite"):
        facade.resolve_symbol("execute", repository=scope, deadline=float("inf"))


TYPE_CALLS = {
    "times-out": _raise(TimeoutError("type")),
    "fails": _raise(RuntimeError("type")),
    "reports": lambda anchor, *, deadline: ProviderLocations((), "provider_reported", False),
    "unsupported": lambda anchor, *, deadline: ProviderLocations((), "unsupported", True),
}
HOVER_CALLS = {
    "fails": _raise(RuntimeError("hover")),
    "empty": lambda anchor, *, deadline: ProviderHover(None, None, True),
    "text": lambda anchor, *, deadline: ProviderHover("doc", None, False),
    "ranged": lambda anchor, *, deadline: ProviderHover(
        "doc", LspRange(LspPosition(0, 0), LspPosition(0, 3)), False
    ),
}
TYPE_CASES = {
    ("times-out", "fails"): ("timeout", ("provider request timed out",)),
    ("fails", "empty"): ("error", ("provider request failed",)),
    ("times-out", "text"): ("partial", ("provider reported partial results",)),
    ("unsupported", "empty"): ("unsupported", ("provider capability is unsupported",)),
    ("reports", "ranged"): ("ok", ()),
}


@pytest.mark.parametrize(("type_call", "hover_call"), sorted(TYPE_CASES))
def test_type_query_combines_type_and_hover_failures(
    navigation, monkeypatch: pytest.MonkeyPatch, type_call: str, hover_call: str
) -> None:
    facade, session, scope = navigation
    _pin_attempt(monkeypatch, session, scope)
    monkeypatch.setattr(facade, "_structural_candidates", None)
    monkeypatch.setattr(session, "type_definition", TYPE_CALLS[type_call])
    monkeypatch.setattr(session, "hover", HOVER_CALLS[hover_call])

    result = facade.query(
        NavigationRequest(scope, Capability.TYPES, "pkg/service.py", 10, 20),
        deadline=time.monotonic() + 5,
    )

    assert (result.status.value, result.warnings) == TYPE_CASES[(type_call, hover_call)]


@pytest.mark.parametrize(
    ("coverage", "expected"),
    [("provider_reported", "lsp_and_graph"), ("unsupported", "lsp_and_graph")],
)
def test_outgoing_calls_ask_the_outgoing_hierarchy(
    navigation, monkeypatch: pytest.MonkeyPatch, coverage: str, expected: str
) -> None:
    facade, session, scope = navigation
    _pin_attempt(monkeypatch, session, scope)
    target = resolve_repository_source(scope, "pkg/api.py")
    location = LspLocation(target.uri, LspRange(LspPosition(0, 6), LspPosition(0, 15)))
    monkeypatch.setattr(
        session,
        "outgoing_calls",
        lambda anchor, *, deadline: ProviderCalls("outgoing", (location,), coverage, False),
    )
    monkeypatch.setattr(session, "incoming_calls", _raise(AssertionError("incoming asked")))
    monkeypatch.setattr(
        session,
        "references",
        lambda anchor, *, deadline: ProviderLocations((location,), "provider_reported", False),
    )

    result = facade.query(
        NavigationRequest(
            scope, Capability.CALLS, "pkg/service.py", 10, 20, direction="outgoing"
        ),
        deadline=time.monotonic() + 5,
    )

    assert result.resolution.value == expected
    assert [item.path for item in result.locations] == ["pkg/api.py"]


def _consumed_documents(scope: RepositoryScope) -> code_navigation._AttemptDocuments:
    documents = code_navigation._AttemptDocuments()
    for path in ("pkg/service.py", "pkg/api.py"):
        source = resolve_repository_source(scope, path)
        content = source.absolute_path.read_bytes()
        documents.consume(source.uri, SourceDocument.from_bytes(path, content))
    return documents


@pytest.mark.parametrize(("entries", "cached"), [(1, ["api.py"]), (2, ["service.py", "api.py"])])
def test_source_document_cache_replaces_and_evicts_by_entry_bound(
    navigation, monkeypatch: pytest.MonkeyPatch, entries: int, cached: list[str]
) -> None:
    facade, _session, scope = navigation
    monkeypatch.setattr(code_navigation, "_MAX_SOURCE_DOCUMENT_CACHE_ENTRIES", entries)
    revision_entries = _revision_entries(scope)
    sizes = []
    for _round in range(3):
        facade._publish_source_documents(revision_entries, _consumed_documents(scope))
        sizes.append(facade._source_document_cache_bytes)

    assert _cached_names(facade) == cached
    assert len(set(sizes)) == 1


def _revision_entries(scope: RepositoryScope) -> dict[str, object]:
    revision = code_navigation._compute_revision(scope, deadline=time.monotonic() + 5)
    return {entry.path: entry for entry in revision.entries}


def _cached_names(facade: CodeNavigation) -> list[str]:
    return [key[0].rsplit("/", 1)[-1] for key in facade._source_document_cache]
