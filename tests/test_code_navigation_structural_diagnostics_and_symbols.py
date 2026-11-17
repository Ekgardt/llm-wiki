"""Navigation caps and merges structural facts, carries provider diagnostics exactly, resolves symbols and verifies edges against a fresh revision."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import code_navigation
import pytest
from code_intelligence import (
    Capability,
    DiagnosticSeverity,
    PositionEncoding,
    PositionRange,
)
from code_navigation import (
    NavigationLocation,
    NavigationRequest,
    NavigationStatus,
    ResolutionLabel,
)
from lsp_positions import LspPosition, LspRange, SourceAnchor, SourceDocument
from lsp_security import resolve_repository_source
from pyright_session import (
    LspDiagnostic,
    LspLocation,
    ProviderDiagnostics,
    ProviderLocations,
)
from repository_scope import RepositoryScope, resolve_repository_scope
from workspace_revision import WorkspaceRevision

from tests.code_kernel_helpers import (
    SemanticPyrightFixture,
)
from tests.code_navigation_helpers import (
    _navigation,
    repository,  # noqa: F401
    semantic_pyright,  # noqa: F401
    state_root,  # noqa: F401
)
from tests.code_navigation_query_helpers import (
    _graph_location,
    _navigation_operation,
    _open_document,
    _patch_empty_definitions,
    _patch_stable_attempt,
    _revision,
    _source_anchor,
)
from tests.slow_machine import SHORT_TIMEOUT


def test_matching_graph_fact_upgrades_lsp_and_graph_only_fact_appends_after_lsp(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        return (
            _graph_location("pkg/api.py", 6, 15),
            _graph_location("pkg/api.py", 0, 5),
        )

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                ),
            ),
            "provider_reported",
            False,
        ),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert len(result.locations) == 2
        confirmed, candidate = result.locations
        assert confirmed.range == PositionRange(6, 15)
        assert confirmed.resolution is ResolutionLabel.LSP_AND_GRAPH
        assert {item.provider for item in confirmed.provenance} == {
            "pyright",
            "evidence-graph",
        }
        assert candidate.range == PositionRange(0, 5)
        assert candidate.resolution is ResolutionLabel.GRAPH_CANDIDATE
        assert {item.provider for item in result.provenance} == {
            "pyright",
            "evidence-graph",
        }
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_structural_fact_count_is_capped_at_ten_thousand(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    many_path = repository / "pkg/many.py"
    many_path.write_bytes(b"x" * 10_001 + b"\n")
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        return tuple(
            _graph_location("pkg/many.py", offset, offset)
            for offset in range(10_001)
        )

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (), "provider_reported", False
        ),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.total == 10_000
        assert len(result.locations) == 10_000
        assert "limit" in " ".join(result.warnings)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def _candidate_slots(operation: str, structural, resolver) -> dict:
    """The seam this operation reads its structural candidates from."""
    if operation == "query":
        return {"structural": structural, "resolver": None}
    return {"structural": None, "resolver": resolver}


def _bounded_result(monkeypatch, session, navigation, scope, operation, deadline):
    """Run the operation, synchronizing the session when the query path needs it."""
    if operation == "query":
        _patch_empty_definitions(
            monkeypatch, session, _open_document(scope, "pkg/service.py")
        )
    return _navigation_operation(operation, navigation, scope, deadline)


@pytest.mark.parametrize("operation", ["query", "resolve_symbol"])
def test_structural_cap_is_applied_after_unique_deduplication(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    candidates = (
        _graph_location("pkg/api.py", 0, 5),
        _graph_location("pkg/api.py", 0, 5),
        _graph_location("pkg/api.py", 6, 15),
    )
    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        **_candidate_slots(
            operation,
            lambda request, deadline: candidates,
            lambda symbol, repository, deadline: candidates,
        ),
    )
    monkeypatch.setattr(code_navigation, "MAX_NAVIGATION_FACTS", 2)
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        result = _bounded_result(
            monkeypatch,
            session,
            navigation,
            scope,
            operation,
            time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.total == 2
        assert [location.range for location in result.locations] == [
            PositionRange(0, 5),
            PositionRange(6, 15),
        ]
        assert "limit" not in " ".join(result.warnings)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize("operation", ["query", "resolve_symbol"])
def test_structural_callback_input_is_bounded_before_fact_deduplication(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")

    def candidates(*args):
        for _ in range(4):
            yield _graph_location("pkg/api.py", 0, 5)
        raise AssertionError("callback iterator consumed past its input bound")

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=candidates if operation == "query" else None,
        resolver=candidates if operation == "resolve_symbol" else None,
    )
    monkeypatch.setattr(
        code_navigation, "MAX_NAVIGATION_INPUT_VALUES", 3, raising=False
    )
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        if operation == "query":
            document = _open_document(scope, "pkg/service.py")
            session._position_encoding = PositionEncoding.UTF8
            monkeypatch.setattr(
                session, "synchronize", lambda value, *, deadline: None
            )
            monkeypatch.setattr(
                session, "open_document", lambda path, *, deadline: document
            )
            monkeypatch.setattr(
                session,
                "definition",
                lambda anchor, *, deadline: ProviderLocations(
                    (), "provider_reported", False
                ),
            )
            result = navigation.query(
                NavigationRequest(
                    scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
                ),
                deadline=time.monotonic() + SHORT_TIMEOUT,
            )
        else:
            result = navigation.resolve_symbol(
                "PublicApi",
                repository=scope,
                deadline=None,
            )
        assert result.status is NavigationStatus.PARTIAL
        assert result.total == 1
        assert "input bound" in " ".join(result.warnings)
        assert "failed" not in " ".join(result.warnings)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_diagnostics_preserve_severity_code_message_version_related_and_partial(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py", version=7)
    api = resolve_repository_source(scope, "pkg/api.py")
    external_uri = (repository.parent / "diagnostic-secret.py").resolve().as_uri()
    diagnostic_range = LspRange(LspPosition(9, 15), LspPosition(9, 25))
    related_range = LspRange(LspPosition(1, 8), LspPosition(1, 14))
    valid = LspDiagnostic(
        document.source.uri,
        diagnostic_range,
        2,
        "reportUnknownMemberType",
        "Member type is unknown",
        (
            (LspLocation(api.uri, related_range), "Declared here"),
            (
                LspLocation(
                    external_uri,
                    LspRange(LspPosition(0, 0), LspPosition(0, 1)),
                ),
                "External",
            ),
        ),
    )
    missing_severity = LspDiagnostic(
        document.source.uri,
        diagnostic_range,
        None,
        "missing",
        "Missing severity",
        (),
    )
    invalid_severity = LspDiagnostic(
        document.source.uri,
        diagnostic_range,
        5,
        "invalid",
        "Invalid severity",
        (),
    )
    boolean_severity = LspDiagnostic(
        document.source.uri,
        diagnostic_range,
        True,
        "boolean",
        "Boolean severity",
        (),
    )
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "diagnostics",
        lambda path, *, deadline: ProviderDiagnostics(
            (valid, missing_severity, invalid_severity, boolean_severity, valid),
            7,
            True,
        ),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DIAGNOSTICS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.document_version == 7
        assert result.total == 1
        assert result.locations == ()
        assert len(result.diagnostics) == 1
        diagnostic = result.diagnostics[0]
        assert diagnostic.path == "pkg/service.py"
        assert diagnostic.severity is DiagnosticSeverity.WARNING
        assert diagnostic.code == "reportUnknownMemberType"
        assert diagnostic.message == "Member type is unknown"
        assert diagnostic.range == PositionRange(216, 226)
        assert len(diagnostic.related) == 1
        assert diagnostic.related[0].path == "pkg/api.py"
        assert diagnostic.related[0].range == PositionRange(25, 31)
        assert all("diagnostic-secret" not in warning for warning in result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize(
    ("severity", "expected"),
    [
        (1, DiagnosticSeverity.ERROR),
        (2, DiagnosticSeverity.WARNING),
        (3, DiagnosticSeverity.INFORMATION),
        (4, DiagnosticSeverity.HINT),
    ],
)
def test_lsp_diagnostic_severity_mapping_is_exact(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    severity: int,
    expected: DiagnosticSeverity,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py", version=3)
    value = LspDiagnostic(
        document.source.uri,
        LspRange(LspPosition(0, 0), LspPosition(0, 4)),
        severity,
        None,
        "diagnostic",
        (),
    )
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "diagnostics",
        lambda path, *, deadline: ProviderDiagnostics((value,), 3, False),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DIAGNOSTICS, "pkg/service.py", 1, 0
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is NavigationStatus.OK
        assert result.diagnostics[0].severity is expected
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_diagnostic_key_distinguishes_missing_and_empty_codes(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py", version=3)
    range_ = LspRange(LspPosition(0, 0), LspPosition(0, 4))
    diagnostics = (
        LspDiagnostic(
            document.source.uri,
            range_,
            1,
            None,
            "same message",
            (),
        ),
        LspDiagnostic(
            document.source.uri,
            range_,
            1,
            "",
            "same message",
            (),
        ),
    )
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "diagnostics",
        lambda path, *, deadline: ProviderDiagnostics(diagnostics, 3, False),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DIAGNOSTICS, "pkg/service.py", 1, 0
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is NavigationStatus.OK
        assert result.total == 2
        assert {diagnostic.code for diagnostic in result.diagnostics} == {None, ""}
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_empty_unversioned_provider_diagnostics_are_not_ready_not_empty_ok(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "diagnostics",
        lambda path, *, deadline: ProviderDiagnostics((), None, True),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DIAGNOSTICS, "pkg/service.py", 1, 0
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is NavigationStatus.NOT_READY
        assert result.total == 0
        assert result.diagnostics == ()
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_diagnostic_and_related_facts_share_one_global_cap(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py", version=4)
    api = resolve_repository_source(scope, "pkg/api.py")
    related = tuple(
        (
            LspLocation(
                api.uri,
                LspRange(LspPosition(0, character), LspPosition(0, character)),
            ),
            None,
        )
        for character in range(3)
    )
    value = LspDiagnostic(
        document.source.uri,
        LspRange(LspPosition(0, 0), LspPosition(0, 4)),
        1,
        None,
        "diagnostic",
        related,
    )
    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=lambda request, deadline: (
            _graph_location("pkg/api.py", 6, 15),
        ),
    )
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(code_navigation, "MAX_NAVIGATION_FACTS", 3)
    monkeypatch.setattr(
        session,
        "diagnostics",
        lambda path, *, deadline: ProviderDiagnostics((value,), 4, False),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DIAGNOSTICS, "pkg/service.py", 1, 0
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.total == 1
        assert 1 + len(result.diagnostics[0].related) == 3
        assert result.locations == ()
        assert "filtered" in " ".join(result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_resolve_symbol_returns_ambiguity_for_multiple_candidates(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    candidates = (
        _graph_location("pkg/api.py", 6, 15),
        _graph_location("pkg/service.py", 0, 4),
    )
    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=lambda symbol, repo, deadline: candidates,
    )
    try:
        result = navigation.resolve_symbol(
            "execute", repository=navigation.repository
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.resolution is ResolutionLabel.AMBIGUOUS
        assert result.total == 2
        assert "disambiguation" in result.warnings[0]
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_resolve_symbol_returns_single_graph_confirmed(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    candidates = (_graph_location("pkg/api.py", 6, 15),)
    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=lambda symbol, repo, deadline: candidates,
    )
    try:
        result = navigation.resolve_symbol(
            "unique", repository=navigation.repository
        )
        assert result.status is NavigationStatus.OK
        assert result.resolution is ResolutionLabel.GRAPH_CONFIRMED
        assert result.total == 1
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_resolve_symbol_unresolved_when_no_candidates(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=lambda symbol, repo, deadline: (),
    )
    try:
        result = navigation.resolve_symbol(
            "missing", repository=navigation.repository
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.resolution is ResolutionLabel.UNRESOLVED
        assert result.total == 0
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize("mutate_every_attempt", [False, True])
def test_resolve_symbol_retries_freshness_once_and_discards_second_race(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    mutate_every_attempt: bool,
) -> None:
    revisions = (
        _revision(repository, "a"),
        _revision(repository, "b"),
        _revision(repository, "c"),
    )
    current_revision = 0
    resolver_calls = 0
    deadlines: list[float | None] = []

    def compute(
        repository: RepositoryScope, *, deadline: float | None
    ) -> WorkspaceRevision:
        return revisions[current_revision]

    def resolver(
        symbol: str, repository: RepositoryScope, deadline: float | None
    ) -> tuple[NavigationLocation, ...]:
        nonlocal current_revision, resolver_calls
        resolver_calls += 1
        assert symbol == "PublicApi"
        deadlines.append(deadline)
        if resolver_calls == 1 or mutate_every_attempt:
            current_revision += 1
        return (_graph_location("pkg/api.py", 6, 15),)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=resolver,
    )
    monkeypatch.setattr(code_navigation, "compute_revision", compute)
    absolute_deadline = time.monotonic() + 5
    try:
        result = navigation.resolve_symbol(
            "PublicApi",
            repository=navigation.repository,
            deadline=absolute_deadline,
        )
        assert resolver_calls == 2
        assert deadlines == [absolute_deadline, absolute_deadline]
        if mutate_every_attempt:
            assert result.status is NavigationStatus.STALE
            assert result.locations == ()
            assert result.provenance == ()
        else:
            assert result.status is NavigationStatus.OK
            assert result.locations[0].resolution is ResolutionLabel.GRAPH_CONFIRMED
            assert result.locations[0].line == 1
            assert result.locations[0].character == 6
            assert result.workspace_revision_before == revisions[1].revision_sha256
            assert result.workspace_revision_after == revisions[1].revision_sha256
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_resolve_symbol_propagates_keyboard_interrupt(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = _revision(repository, "a")

    def interrupted(
        symbol: str, repository: RepositoryScope, deadline: float | None
    ) -> tuple[NavigationLocation, ...]:
        raise KeyboardInterrupt

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=interrupted,
    )
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            navigation.resolve_symbol(
                "PublicApi",
                repository=navigation.repository,
                deadline=time.monotonic() + 5,
            )
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_resolve_symbol_stable_sorts_dedupes_validates_and_marks_ambiguity(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = _revision(repository, "a")
    candidates = (
        _graph_location("pkg/api.py", 6, 15),
        _graph_location("pkg/missing.py", 0, 1),
        _graph_location("pkg/api.py", 0, 5),
        _graph_location("pkg/api.py", 6, 15),
    )
    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=lambda symbol, scope, deadline: candidates,
    )
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        result = navigation.resolve_symbol(
            "PublicApi",
            repository=navigation.repository,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.resolution is ResolutionLabel.AMBIGUOUS
        assert [location.range for location in result.locations] == [
            PositionRange(0, 5),
            PositionRange(6, 15),
        ]
        assert all(
            location.resolution is ResolutionLabel.AMBIGUOUS
            for location in result.locations
        )
        assert "filtered" in " ".join(result.warnings)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_resolve_symbol_caps_candidates_and_reports_limit(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repository / "pkg/many.py").write_bytes(b"x" * 10_001 + b"\n")
    revision = _revision(repository, "a")

    def resolver(
        symbol: str, scope: RepositoryScope, deadline: float | None
    ) -> tuple[NavigationLocation, ...]:
        return tuple(
            _graph_location("pkg/many.py", offset, offset)
            for offset in range(10_001)
        )

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=resolver,
    )
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        result = navigation.resolve_symbol(
            "many",
            repository=navigation.repository,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.total == 10_000
        assert len(result.locations) == 10_000
        assert all(
            location.resolution is ResolutionLabel.AMBIGUOUS
            for location in result.locations
        )
        assert "limit" in " ".join(result.warnings)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_resolve_symbol_input_bound_never_confirms_truncated_singleton(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = _revision(repository, "a")
    candidates = (
        _graph_location("pkg/api.py", 6, 15),
        _graph_location("pkg/service.py", 0, 4),
    )
    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=lambda symbol, scope, deadline: candidates,
    )
    monkeypatch.setattr(code_navigation, "MAX_NAVIGATION_INPUT_VALUES", 1)
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        result = navigation.resolve_symbol(
            "PublicApi",
            repository=navigation.repository,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.total == 1
        assert result.resolution is ResolutionLabel.AMBIGUOUS
        assert result.locations[0].resolution is ResolutionLabel.AMBIGUOUS
        assert "input bound" in " ".join(result.warnings)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_resolve_symbol_rejects_empty_revision_without_publishing_candidates(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = replace(_revision(repository, "a"), revision_sha256="")
    calls = 0

    def resolver(
        symbol: str, scope: RepositoryScope, deadline: float | None
    ) -> tuple[NavigationLocation, ...]:
        nonlocal calls
        calls += 1
        return (_graph_location("pkg/api.py", 6, 15),)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=resolver,
    )
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        result = navigation.resolve_symbol(
            "PublicApi",
            repository=navigation.repository,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is NavigationStatus.ERROR
        assert result.locations == ()
        assert calls == 0
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize("verified", [False, True])
def test_verify_edge_uses_both_anchors_and_only_true_confirms_target(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    verified: bool,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    source = _source_anchor(scope, "pkg/service.py", 10, 15)
    target = _source_anchor(scope, "pkg/api.py", 2, 8)
    calls: list[tuple[SourceAnchor, SourceAnchor, RepositoryScope, float]] = []

    def verifier(
        source_anchor: SourceAnchor,
        target_anchor: SourceAnchor,
        repository_scope: RepositoryScope,
        deadline: float,
    ) -> bool:
        calls.append((source_anchor, target_anchor, repository_scope, deadline))
        return verified

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        edge=verifier,
    )
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    absolute_deadline = time.monotonic() + 5
    before_files = {path.relative_to(state_root) for path in state_root.rglob("*")}
    try:
        result = navigation.verify_edge(
            source,
            target,
            repository=scope,
            deadline=absolute_deadline,
        )
        assert calls == [(source, target, scope, absolute_deadline)]
        assert {path.relative_to(state_root) for path in state_root.rglob("*")} == before_files
        if verified:
            assert result.status is NavigationStatus.OK
            assert result.resolution is ResolutionLabel.GRAPH_CONFIRMED
            assert len(result.locations) == 1
            location = result.locations[0]
            assert location.path == target.path
            assert location.range == PositionRange(target.byte_offset, target.byte_offset)
            assert location.line == target.line
            assert location.character == target.utf8_character
        else:
            assert result.status is NavigationStatus.PARTIAL
            assert result.resolution is ResolutionLabel.UNRESOLVED
            assert result.locations == ()
            assert "no structural edge proof" in result.warnings
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_verify_edge_without_verifier_is_partial_not_deadness_proof(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    source = _source_anchor(scope, "pkg/service.py", 10, 15)
    target = _source_anchor(scope, "pkg/api.py", 2, 8)
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        result = navigation.verify_edge(
            source,
            target,
            repository=scope,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.resolution is ResolutionLabel.UNRESOLVED
        assert result.locations == ()
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize("mutate_every_attempt", [False, True])
def test_verify_edge_retries_freshness_once_and_discards_second_race(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    mutate_every_attempt: bool,
) -> None:
    scope = resolve_repository_scope(repository)
    revisions = (
        _revision(repository, "a"),
        _revision(repository, "b"),
        _revision(repository, "c"),
    )
    source = _source_anchor(scope, "pkg/service.py", 10, 15)
    target = _source_anchor(scope, "pkg/api.py", 2, 8)
    current_revision = 0
    verifier_calls = 0

    def compute(repository: RepositoryScope, *, deadline: float) -> WorkspaceRevision:
        return revisions[current_revision]

    def verifier(
        source_anchor: SourceAnchor,
        target_anchor: SourceAnchor,
        repository_scope: RepositoryScope,
        deadline: float,
    ) -> bool:
        nonlocal current_revision, verifier_calls
        verifier_calls += 1
        assert source_anchor is source
        assert target_anchor is target
        if verifier_calls == 1 or mutate_every_attempt:
            current_revision += 1
        return True

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        edge=verifier,
    )
    monkeypatch.setattr(code_navigation, "compute_revision", compute)
    try:
        result = navigation.verify_edge(
            source,
            target,
            repository=scope,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert verifier_calls == 2
        if mutate_every_attempt:
            assert result.status is NavigationStatus.STALE
            assert result.locations == ()
            assert result.provenance == ()
        else:
            assert result.status is NavigationStatus.OK
            assert result.workspace_revision_before == revisions[1].revision_sha256
            assert result.workspace_revision_after == revisions[1].revision_sha256
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize(
    "failure_type",
    [FileNotFoundError, PermissionError, OSError],
)
@pytest.mark.parametrize("mutate_every_attempt", [False, True])
def test_verify_edge_retries_resolution_failure_for_pre_revision_entry(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[OSError],
    mutate_every_attempt: bool,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    source = _source_anchor(scope, "pkg/service.py", 10, 15)
    target = _source_anchor(scope, "pkg/api.py", 2, 8)
    original_resolve = code_navigation.resolve_repository_source
    target_resolutions = 0
    verifier_calls = 0

    def resolve(
        repository_scope: RepositoryScope, path: str
    ):
        nonlocal target_resolutions
        if path == target.path:
            target_resolutions += 1
            if target_resolutions == 1 or mutate_every_attempt:
                raise failure_type("target changed after revision")
        return original_resolve(repository_scope, path)

    def verifier(*args) -> bool:
        nonlocal verifier_calls
        verifier_calls += 1
        return True

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        edge=verifier,
    )
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    monkeypatch.setattr(code_navigation, "resolve_repository_source", resolve)
    try:
        result = navigation.verify_edge(
            source,
            target,
            repository=scope,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        if mutate_every_attempt:
            assert target_resolutions == 2
            assert verifier_calls == 0
            assert result.status is NavigationStatus.STALE
            assert result.locations == ()
            assert result.provenance == ()
        else:
            assert target_resolutions == 2
            assert verifier_calls == 1
            assert result.status is NavigationStatus.OK
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_verify_edge_missing_from_fresh_revision_is_error_after_post_fence(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    source = _source_anchor(scope, "pkg/service.py", 10, 15)
    target = replace(_source_anchor(scope, "pkg/api.py", 2, 8), path="pkg/missing.py")
    revision_calls = 0

    def compute(
        repository_scope: RepositoryScope, *, deadline: float
    ) -> WorkspaceRevision:
        nonlocal revision_calls
        revision_calls += 1
        return revision

    navigation, session = _navigation(repository, state_root, semantic_pyright)
    monkeypatch.setattr(code_navigation, "compute_revision", compute)
    try:
        result = navigation.verify_edge(
            source,
            target,
            repository=scope,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is NavigationStatus.ERROR
        assert revision_calls == 2
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize("appeared_anchor", ["source", "target"])
def test_verify_edge_retries_when_either_anchor_first_appears_inside_revision_fence(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    appeared_anchor: str,
) -> None:
    scope = resolve_repository_scope(repository)
    relative_path = f"pkg/appeared_{appeared_anchor}.py"
    target_path = repository / relative_path
    content = b"appeared = True\n"
    appeared = SourceDocument.from_bytes(relative_path, content).validate_anchor(
        line=1,
        character=0,
    )
    existing_source = _source_anchor(scope, "pkg/service.py", 10, 15)
    existing_target = _source_anchor(scope, "pkg/api.py", 2, 8)
    source = appeared if appeared_anchor == "source" else existing_source
    target = appeared if appeared_anchor == "target" else existing_target
    original_resolve = code_navigation.resolve_repository_source
    verifier_calls = 0

    def resolve(repository_scope: RepositoryScope, path: str):
        if path == relative_path and not target_path.exists():
            target_path.write_bytes(content)
        return original_resolve(repository_scope, path)

    def verifier(*args) -> bool:
        nonlocal verifier_calls
        verifier_calls += 1
        return True

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        edge=verifier,
    )
    monkeypatch.setattr(code_navigation, "resolve_repository_source", resolve)
    try:
        result = navigation.verify_edge(
            source,
            target,
            repository=scope,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )

        assert result.status is NavigationStatus.OK
        assert result.workspace_revision_before == result.workspace_revision_after
        assert verifier_calls == 1
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_verify_edge_validates_both_anchors_before_callback_and_propagates_interrupt(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    source = _source_anchor(scope, "pkg/service.py", 10, 15)
    target = _source_anchor(scope, "pkg/api.py", 2, 8)
    calls = 0

    def interrupted(
        source_anchor: SourceAnchor,
        target_anchor: SourceAnchor,
        repository_scope: RepositoryScope,
        deadline: float,
    ) -> bool:
        nonlocal calls
        calls += 1
        raise KeyboardInterrupt

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        edge=interrupted,
    )
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        invalid_target = replace(target, byte_offset=target.byte_offset + 1)
        result = navigation.verify_edge(
            source,
            invalid_target,
            repository=scope,
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.ERROR
        assert calls == 0
        with pytest.raises(KeyboardInterrupt):
            navigation.verify_edge(
                source,
                target,
                repository=scope,
                deadline=time.monotonic() + 5,
            )
    finally:
        session.close(deadline=time.monotonic() + 5)
