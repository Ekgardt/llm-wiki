"""Navigation falls back to fresh structural evidence when the provider fails, and validates every provider target against the revision."""

from __future__ import annotations

import hashlib
import time
from dataclasses import replace
from pathlib import Path

import code_navigation
import pytest
from code_intelligence import (
    Capability,
    PositionEncoding,
    PositionRange,
)
from code_navigation import (
    NavigationLocation,
    NavigationRequest,
    NavigationStatus,
    ResolutionLabel,
)
from lsp_positions import LspPosition, LspRange, SourceAnchor
from lsp_security import resolve_repository_source
from pyright_session import (
    LspLocation,
    OpenDocument,
    ProviderLocations,
)
from repository_scope import RepositoryScope, resolve_repository_scope
from workspace_revision import WorkspaceRevision

from tests.code_kernel_helpers import (
    SemanticPyrightFixture,
    create_semantic_pyright_fixture,
)
from tests.code_navigation_helpers import (
    _navigation,
    repository,  # noqa: F401
    semantic_pyright,  # noqa: F401
    state_root,  # noqa: F401
)
from tests.code_navigation_query_helpers import (
    _callback_slots,
    _graph_location,
    _navigation_operation,
    _open_document,
    _patch_empty_definitions,
    _patch_stable_attempt,
    _revision,
)
from tests.slow_machine import SHORT_TIMEOUT


@pytest.mark.parametrize(
    ("failure", "expected", "expected_callbacks"),
    [
        (TimeoutError("late"), NavigationStatus.TIMEOUT, 0),
        (RuntimeError("D:/private/secret.py"), NavigationStatus.PARTIAL, 1),
    ],
)
def test_provider_failure_still_uses_deadline_aware_structural_fallback(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected: NavigationStatus,
    expected_callbacks: int,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    seen_deadlines: list[float] = []

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        seen_deadlines.append(deadline)
        return (_graph_location("pkg/api.py", 6, 15),)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    _patch_stable_attempt(monkeypatch, session, revision, document)

    def fail(_anchor, *, deadline: float) -> ProviderLocations:
        raise failure

    monkeypatch.setattr(session, "definition", fail)
    absolute_deadline = time.monotonic() + 5
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=absolute_deadline,
        )
        assert result.status is expected
        assert len(seen_deadlines) == expected_callbacks
        if expected_callbacks == 0:
            assert result.locations == ()
        else:
            assert seen_deadlines == [absolute_deadline]
            assert len(result.locations) == 1
            assert result.locations[0].resolution is ResolutionLabel.GRAPH_CANDIDATE
        assert all("private" not in warning and "secret" not in warning for warning in result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("stage", ["synchronize", "open_document"])
def test_provider_setup_failure_publishes_only_fresh_structural_fallback(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    callbacks = 0

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        nonlocal callbacks
        callbacks += 1
        return (_graph_location("pkg/api.py", 6, 15),)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    _patch_stable_attempt(monkeypatch, session, revision, document)
    session._readiness = "query_ready"

    def fail(*args, **kwargs):
        raise RuntimeError("D:/private/provider-sensitive.py")

    monkeypatch.setattr(session, stage, fail)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert callbacks == 1
        assert result.status is NavigationStatus.PARTIAL
        assert result.effective_capability is None
        assert result.readiness == "query_ready"
        assert len(result.locations) == 1
        assert result.locations[0].resolution is ResolutionLabel.GRAPH_CANDIDATE
        assert {item.provider for item in result.provenance} == {"evidence-graph"}
        assert "setup" in " ".join(result.warnings)
        assert all("private" not in warning for warning in result.warnings)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize("stage", ["synchronize", "open_document"])
@pytest.mark.parametrize(
    ("request_path", "request_line"),
    [("pkg/missing.py", 1), ("pkg/service.py", 1_000)],
)
def test_setup_failure_never_bypasses_request_source_or_anchor_validation(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    request_path: str,
    request_line: int,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    structural_calls = 0

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        nonlocal structural_calls
        structural_calls += 1
        return (_graph_location("pkg/api.py", 6, 15),)

    def fail_setup(*args, **kwargs):
        raise RuntimeError("provider setup failed")

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(
        session,
        "open_document",
        lambda path, *, deadline: _open_document(scope, "pkg/service.py"),
    )
    monkeypatch.setattr(session, stage, fail_setup)
    try:
        result = navigation.query(
            NavigationRequest(
                scope,
                Capability.DEFINITIONS,
                request_path,
                request_line,
                0,
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )

        assert result.status is NavigationStatus.ERROR
        assert result.locations == ()
        assert result.provenance == ()
        assert structural_calls == 0
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_setup_failure_retries_request_source_disk_mismatch_before_fallback(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    (repository / "pkg/service.py").write_bytes(b"changed_after_revision = True\n")
    revision_calls = 0
    structural_calls = 0

    def compute(
        repository_scope: RepositoryScope, *, deadline: float
    ) -> WorkspaceRevision:
        nonlocal revision_calls
        revision_calls += 1
        return revision

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        nonlocal structural_calls
        structural_calls += 1
        return (_graph_location("pkg/api.py", 6, 15),)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    monkeypatch.setattr(code_navigation, "compute_revision", compute)
    monkeypatch.setattr(
        session,
        "synchronize",
        lambda value, *, deadline: (_ for _ in ()).throw(RuntimeError("not ready")),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope,
                Capability.DEFINITIONS,
                "pkg/service.py",
                1,
                0,
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )

        assert result.status is NavigationStatus.STALE
        assert result.locations == ()
        assert result.provenance == ()
        assert revision_calls == 2
        assert structural_calls == 0
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize("mutate_every_attempt", [False, True])
def test_setup_failure_validates_after_synchronize_before_aba_fallback(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    mutate_every_attempt: bool,
) -> None:
    scope = resolve_repository_scope(repository)
    source_path = repository / "pkg/service.py"
    content_a = source_path.read_bytes()
    content_b = b"changed_by_synchronize = True\n"
    revision_a = _revision(repository, "a")
    source_path.write_bytes(content_b)
    revision_b = _revision(repository, "b")
    source_path.write_bytes(content_a)
    revisions = {content_a: revision_a, content_b: revision_b}
    synchronize_calls = 0
    structural_calls = 0
    events: list[str] = []

    def compute(
        repository_scope: RepositoryScope, *, deadline: float
    ) -> WorkspaceRevision:
        return revisions[source_path.read_bytes()]

    def synchronize(value: WorkspaceRevision, *, deadline: float) -> None:
        nonlocal synchronize_calls
        synchronize_calls += 1
        events.append(f"synchronize:{synchronize_calls}")
        if synchronize_calls == 1:
            assert value is revision_a
            source_path.write_bytes(content_b)
        else:
            assert value is revision_b
            if mutate_every_attempt:
                source_path.write_bytes(content_a)
        raise RuntimeError("provider setup failed after mutation")

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        nonlocal structural_calls
        structural_calls += 1
        events.append("structural")
        if synchronize_calls == 1:
            source_path.write_bytes(content_a)
        return (_graph_location("pkg/api.py", 6, 15),)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    monkeypatch.setattr(code_navigation, "compute_revision", compute)
    monkeypatch.setattr(session, "synchronize", synchronize)
    try:
        result = navigation.query(
            NavigationRequest(
                scope,
                Capability.DEFINITIONS,
                "pkg/service.py",
                1,
                0,
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )

        assert synchronize_calls == 2
        if mutate_every_attempt:
            assert events == ["synchronize:1", "synchronize:2"]
            assert structural_calls == 0
            assert result.status is NavigationStatus.STALE
            assert result.locations == ()
            assert result.provenance == ()
        else:
            assert events == ["synchronize:1", "synchronize:2", "structural"]
            assert structural_calls == 1
            assert result.status is NavigationStatus.PARTIAL
            assert result.workspace_revision_before == revision_b.revision_sha256
            assert result.workspace_revision_after == revision_b.revision_sha256
            assert len(result.locations) == 1
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_query_retries_when_request_source_first_appears_inside_revision_fence(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    relative_path = "pkg/appeared.py"
    target = repository / relative_path
    content = b"appeared = True\n"
    source = resolve_repository_source(scope, relative_path, must_exist=False)
    document = OpenDocument(
        source,
        content,
        hashlib.sha256(content).hexdigest(),
        1,
    )
    original_resolve = code_navigation.resolve_repository_source
    provider_calls = 0

    def resolve(repository_scope: RepositoryScope, path: str):
        if path == relative_path and not target.exists():
            target.write_bytes(content)
        return original_resolve(repository_scope, path)

    def definition(anchor: SourceAnchor, *, deadline: float) -> ProviderLocations:
        nonlocal provider_calls
        provider_calls += 1
        return ProviderLocations((), "provider_reported", False)

    navigation, session = _navigation(repository, state_root, semantic_pyright)
    session._position_encoding = PositionEncoding.UTF8
    monkeypatch.setattr(code_navigation, "resolve_repository_source", resolve)
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(
        session,
        "open_document",
        lambda path, *, deadline: document,
    )
    monkeypatch.setattr(session, "definition", definition)
    try:
        result = navigation.query(
            NavigationRequest(
                scope,
                Capability.DEFINITIONS,
                relative_path,
                1,
                0,
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )

        assert result.status is NavigationStatus.OK
        assert result.workspace_revision_before == result.workspace_revision_after
        assert provider_calls == 1
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize("stage", ["synchronize", "open_document"])
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (RuntimeError("D:/private/provider-not-ready.py"), NavigationStatus.NOT_READY),
        (TimeoutError("D:/private/provider-timeout.py"), NavigationStatus.NOT_READY),
    ],
)
def test_provider_setup_distinguishes_not_ready_from_timeout(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    failure: Exception,
    expected: NavigationStatus,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    session._readiness = "query_ready"

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(session, stage, fail)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is expected
        assert result.effective_capability is None
        assert result.readiness == "query_ready"
        assert result.locations == ()
        assert all("private" not in warning for warning in result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("useful_graph", [False, True])
@pytest.mark.parametrize("mutate_every_attempt", [False, True])
def test_setup_failure_fallback_retries_freshness_before_publication(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    useful_graph: bool,
    mutate_every_attempt: bool,
) -> None:
    scope = resolve_repository_scope(repository)
    revisions = (
        _revision(repository, "a"),
        _revision(repository, "b"),
        _revision(repository, "c"),
    )
    current_revision = 0
    callbacks = 0

    def compute(
        repository: RepositoryScope, *, deadline: float
    ) -> WorkspaceRevision:
        return revisions[current_revision]

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        nonlocal callbacks, current_revision
        callbacks += 1
        if callbacks == 1 or mutate_every_attempt:
            current_revision += 1
        return (
            (_graph_location("pkg/api.py", 6, 15),)
            if useful_graph
            else ()
        )

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    session._readiness = "not_ready"
    monkeypatch.setattr(code_navigation, "compute_revision", compute)
    monkeypatch.setattr(
        session,
        "synchronize",
        lambda value, *, deadline: (_ for _ in ()).throw(RuntimeError("not ready")),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert callbacks == 2
        assert result.effective_capability is None
        if mutate_every_attempt:
            assert result.status is NavigationStatus.STALE
            assert result.locations == ()
            assert result.provenance == ()
            return
        if useful_graph:
            assert result.status is NavigationStatus.PARTIAL
            assert len(result.locations) == 1
            return
        assert result.status is NavigationStatus.NOT_READY
        assert result.locations == ()
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize("stage", ["synchronize", "open_document"])
def test_setup_timeout_after_absolute_deadline_skips_structural_fallback(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    clock = {"now": 1.0}
    real_monotonic = time.monotonic
    callbacks = 0

    def expire(*args, **kwargs):
        clock["now"] = 11.0
        raise TimeoutError("setup timeout")

    def structural(*args):
        nonlocal callbacks
        callbacks += 1
        return (_graph_location("pkg/api.py", 6, 15),)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(session, stage, expire)
    monkeypatch.setattr(code_navigation.time, "monotonic", lambda: clock["now"])
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=10.0,
        )
        assert result.status is NavigationStatus.TIMEOUT
        assert result.effective_capability is None
        assert result.locations == ()
        assert callbacks == 0
    finally:
        monkeypatch.setattr(code_navigation.time, "monotonic", real_monotonic)
        session.close(deadline=real_monotonic() + 5)


def _throwing_revision(error: type[BaseException]):
    def compute(repository, *, deadline):  # noqa: F811
        raise error()

    return compute


def _patch_early_revision(monkeypatch, early_stage: str, revision) -> None:
    """Break the revision the way this stage names, or leave it as it is."""
    replacements = {
        "revision_timeout": _throwing_revision(TimeoutError),
        "revision_error": _throwing_revision(RuntimeError),
        "empty_revision": lambda repository, *, deadline: replace(  # noqa: F811
            revision, revision_sha256=""
        ),
    }
    replacement = replacements.get(early_stage)
    if replacement is None:
        return
    monkeypatch.setattr(code_navigation, "compute_revision", replacement)


def _early_deadline(early_stage: str) -> float:
    if early_stage == "expired":
        return 0.0
    return time.monotonic() + 5


@pytest.mark.parametrize(
    "early_stage",
    ["expired", "revision_timeout", "revision_error", "empty_revision"],
)
def test_early_results_preserve_live_session_readiness(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    early_stage: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    session._readiness = "query_ready"
    _patch_early_revision(monkeypatch, early_stage, revision)
    deadline = _early_deadline(early_stage)
    try:
        result = navigation.query(
            NavigationRequest(scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20),
            deadline=deadline,
        )
        assert result.readiness == "query_ready"
        assert result.effective_capability is None
        assert result.locations == ()
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_setup_failure_structural_interruption_still_propagates(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")

    def interrupted(*args):
        raise code_navigation.NavigationInterruption

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=interrupted,
    )
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    monkeypatch.setattr(
        session,
        "synchronize",
        lambda value, *, deadline: (_ for _ in ()).throw(RuntimeError("not ready")),
    )
    try:
        with pytest.raises(code_navigation.NavigationInterruption):
            navigation.query(
                NavigationRequest(
                    scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
                ),
                deadline=time.monotonic() + 5,
            )
    finally:
        session.close(deadline=time.monotonic() + 5)


_SECOND_CALL_FAILURES = {
    "post_revision_timeout": TimeoutError,
    "post_revision_error": RuntimeError,
}


def _revision_for_stage(stage: str, revisions: tuple, call: int):
    """The revision this call answers with, or the failure the stage asks for."""
    failure = _SECOND_CALL_FAILURES.get(stage)
    if failure is not None and call == 2:
        raise failure
    if stage == "post_revision_stale":
        return revisions[call - 1]
    return revisions[0]


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("source_validation", NavigationStatus.ERROR),
        ("anchor_validation", NavigationStatus.ERROR),
        ("post_revision_timeout", NavigationStatus.TIMEOUT),
        ("post_revision_error", NavigationStatus.ERROR),
        ("post_revision_stale", NavigationStatus.STALE),
    ],
)
def test_post_setup_empty_results_preserve_readiness_and_empty_contract(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    expected: NavigationStatus,
) -> None:
    scope = resolve_repository_scope(repository)
    revisions = tuple(_revision(repository, marker) for marker in "abcd")
    document = _open_document(scope, "pkg/service.py")
    if stage == "source_validation":
        document = replace(document, source_sha256="0" * 64)
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    session._position_encoding = PositionEncoding.UTF8
    session._readiness = "query_ready"
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(
        session,
        "open_document",
        lambda path, *, deadline: document,
    )
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (), "provider_reported", False
        ),
    )
    revision_calls = 0

    def compute(
        repository: RepositoryScope, *, deadline: float
    ) -> WorkspaceRevision:
        nonlocal revision_calls
        revision_calls += 1
        return _revision_for_stage(stage, revisions, revision_calls)

    monkeypatch.setattr(code_navigation, "compute_revision", compute)
    request_line = 1000 if stage == "anchor_validation" else 10
    try:
        result = navigation.query(
            NavigationRequest(
                scope,
                Capability.DEFINITIONS,
                "pkg/service.py",
                request_line,
                20,
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is expected
        assert result.readiness == "query_ready"
        assert result.effective_capability is None
        assert result.total == 0
        assert result.symbol is None
        assert result.hover is None
        assert result.locations == ()
        assert result.diagnostics == ()
        assert result.provenance == ()
        assert isinstance(result.locations, tuple)
        assert isinstance(result.diagnostics, tuple)
        assert isinstance(result.provenance, tuple)
        assert isinstance(result.warnings, tuple)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize(
    ("callback_kind", "expected"),
    [
        ("structural", NavigationStatus.PARTIAL),
        ("resolver", NavigationStatus.ERROR),
        ("edge", NavigationStatus.ERROR),
    ],
)
def test_callback_exceptions_are_redacted_at_every_boundary(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    callback_kind: str,
    expected: NavigationStatus,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")

    def fail(*args, **kwargs):
        raise KeyError("D:/private/callback-secret.py")

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        **_callback_slots(callback_kind, fail),
    )
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        if callback_kind == "structural":
            _patch_empty_definitions(
                monkeypatch, session, _open_document(scope, "pkg/service.py")
            )
        result = _navigation_operation(
            callback_kind, navigation, scope, time.monotonic() + 5
        )
        assert result.status is expected
        assert result.locations == ()
        assert all(
            "private" not in warning and "secret" not in warning
            for warning in result.warnings
        )
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize("callback_kind", ["structural", "resolver", "edge"])
def test_navigation_interruption_propagates_from_every_callback_boundary(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    callback_kind: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")

    def interrupted(*args, **kwargs):
        raise code_navigation.NavigationInterruption

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        **_callback_slots(callback_kind, interrupted),
    )
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        if callback_kind == "structural":
            _patch_empty_definitions(
                monkeypatch, session, _open_document(scope, "pkg/service.py")
            )
        with pytest.raises(code_navigation.NavigationInterruption):
            _navigation_operation(
                callback_kind, navigation, scope, time.monotonic() + 5
            )
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_every_capability_has_a_route() -> None:
    """Audit C-44: no `Capability` exists that a query would have to refuse."""
    assert set(Capability) == set(code_navigation.CAPABILITY_DIRECTION)


@pytest.mark.parametrize(
    ("encoding", "start_character", "end_character"),
    [
        (PositionEncoding.UTF8, 14, 16),
        (PositionEncoding.UTF16, 12, 13),
        (PositionEncoding.UTF32, 11, 12),
    ],
)
def test_cross_file_unicode_targets_use_negotiated_position_encoding(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    encoding: PositionEncoding,
    start_character: int,
    end_character: int,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "capabilities": {
                "definitionProvider": True,
                "documentSymbolProvider": True,
                "positionEncoding": encoding.value,
                "textDocumentSync": 2,
            },
            "responses": {
                "textDocument/definition": [
                    {
                        "uri": "$UNICODE_URI",
                        "range": {
                            "start": {"line": 0, "character": start_character},
                            "end": {"line": 0, "character": end_character},
                        },
                    }
                ]
            },
        },
    )
    navigation, session = _navigation(repository, state_root, fixture)
    try:
        result = navigation.query(
            NavigationRequest(
                resolve_repository_scope(repository),
                Capability.DEFINITIONS,
                "pkg/service.py",
                10,
                20,
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is NavigationStatus.OK
        assert result.position_encoding is encoding
        assert len(result.locations) == 1
        location = result.locations[0]
        assert location.path == "pkg/unicode_api.py"
        assert location.range == PositionRange(14, 16)
        assert location.line == 1
        assert location.character == 14
    finally:
        navigation.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_provider_target_with_stale_pre_revision_hash_returns_stale(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    clean_revision = _revision(repository, "a")
    revision = replace(
        clean_revision,
        entries=tuple(
            replace(entry, sha256="0" * 64)
            if entry.path == "pkg/api.py"
            else entry
            for entry in clean_revision.entries
        ),
    )
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    navigation, session = _navigation(repository, state_root, semantic_pyright)
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
        assert result.status is NavigationStatus.STALE
        assert result.locations == ()
        assert "changed" in " ".join(result.warnings)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


_REPEATED_RANGES = (
    LspRange(LspPosition(0, 0), LspPosition(0, 5)),
    LspRange(LspPosition(0, 6), LspPosition(0, 15)),
    LspRange(LspPosition(1, 4), LspPosition(1, 7)),
    LspRange(LspPosition(1, 8), LspPosition(1, 14)),
    LspRange(LspPosition(2, 8), LspPosition(2, 14)),
)


def _repeated_provider_locations(target, external_uri: str) -> tuple:
    """Five locations inside the repository and two outside it."""
    inside = tuple(LspLocation(target.uri, range_) for range_ in _REPEATED_RANGES)
    outside = (
        LspLocation(external_uri, LspRange(LspPosition(0, 0), LspPosition(0, 1))),
        LspLocation(external_uri, LspRange(LspPosition(0, 1), LspPosition(0, 2))),
    )
    return inside + outside


def _record_normalized_uris(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Collect every provider URI the navigation asks to normalize."""
    real_normalize = code_navigation.normalize_provider_uri
    normalized: list[str] = []

    def recording_normalize(repository_scope: RepositoryScope, uri: str):
        normalized.append(uri)
        return real_normalize(repository_scope, uri)

    monkeypatch.setattr(code_navigation, "normalize_provider_uri", recording_normalize)
    return normalized


def _location_facts(result) -> tuple:
    """Status, count, the files pointed at, and how many distinct ranges."""
    paths = {location.path for location in result.locations}
    ranges = {location.range for location in result.locations}
    return (result.status, len(result.locations), paths, len(ranges))


def test_provider_uri_normalization_is_attempt_local_for_repeated_locations(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    external_uri = (repository.parent / "external-sensitive.py").resolve().as_uri()
    provider_locations = _repeated_provider_locations(target, external_uri)
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            provider_locations, "provider_reported", False
        ),
    )
    normalized_uris = _record_normalized_uris(monkeypatch)
    request = NavigationRequest(
        scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
    )
    try:
        results = tuple(
            navigation.query(request, deadline=time.monotonic() + SHORT_TIMEOUT)
            for _ in range(2)
        )

        for result in results:
            assert _location_facts(result) == (
                NavigationStatus.PARTIAL,
                5,
                {"pkg/api.py"},
                5,
            )
        assert normalized_uris == [
            target.uri,
            external_uri,
            target.uri,
            external_uri,
        ]
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_external_provider_target_is_filtered_without_path_disclosure(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    external_uri = (repository.parent / "external-sensitive.py").resolve().as_uri()
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (
                LspLocation(
                    external_uri,
                    LspRange(LspPosition(0, 0), LspPosition(0, 1)),
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
        assert result.status is NavigationStatus.PARTIAL
        assert result.locations == ()
        assert all("external-sensitive" not in warning for warning in result.warnings)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_structural_targets_are_revision_validated_and_rebuilt(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        return (
            _graph_location("pkg/api.py", 6, 15),
            _graph_location("pkg/missing.py", 0, 1),
            _graph_location("pkg/api.py", 0, 100_000),
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
        assert len(result.locations) == 1
        assert result.locations[0].path == "pkg/api.py"
        assert result.locations[0].line == 1
        assert result.locations[0].character == 6
        assert result.locations[0].resolution is ResolutionLabel.GRAPH_CANDIDATE
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)
