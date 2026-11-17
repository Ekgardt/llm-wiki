"""A navigation query validates first, runs each attempt inside one revision fence and deadline, and never publishes stale or partial facts."""

from __future__ import annotations

import hashlib
import time
from dataclasses import replace
from pathlib import Path

import code_navigation
import pytest
from bounded_io import read_stable_bytes
from code_intelligence import (
    Capability,
    PositionEncoding,
    PositionRange,
)
from code_navigation import (
    NavigationLocation,
    NavigationRequest,
    NavigationResult,
    NavigationStatus,
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
    _callback_slots,
    _graph_location,
    _navigation_operation,
    _open_document,
    _patch_empty_definitions,
    _patch_stable_attempt,
    _revision,
)
from tests.slow_machine import SHORT_TIMEOUT


def test_query_validates_request_before_expired_deadline(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    try:
        with pytest.raises(TypeError, match="NavigationRequest"):
            navigation.query(object(), deadline=0.0)  # type: ignore[arg-type]
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_query_rejects_empty_revision_before_provider_operation(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = replace(_revision(repository, "a"), revision_sha256="")
    document = _open_document(scope, "pkg/service.py")
    provider_calls = 0
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)

    def definition(anchor: SourceAnchor, *, deadline: float) -> ProviderLocations:
        nonlocal provider_calls
        provider_calls += 1
        return ProviderLocations((), "provider_reported", False)

    monkeypatch.setattr(session, "definition", definition)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is NavigationStatus.ERROR
        assert result.locations == ()
        assert provider_calls == 0
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_query_attempt_order_keeps_structural_callback_and_target_reads_in_fence(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    request_document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    events: list[str] = []

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        assert request.path == "pkg/service.py"
        assert deadline == absolute_deadline
        events.append("structural")
        return ()

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    session._position_encoding = PositionEncoding.UTF8
    original_read = read_stable_bytes
    revision_calls = 0

    def compute(_repository: RepositoryScope, *, deadline: float) -> WorkspaceRevision:
        nonlocal revision_calls
        revision_calls += 1
        events.append("pre" if revision_calls == 1 else "post")
        return revision

    def synchronize(value: WorkspaceRevision, *, deadline: float) -> None:
        assert value is revision
        events.append("synchronize")

    def open_document(path: str, *, deadline: float) -> OpenDocument:
        assert path == request_document.source.relative_path
        events.append("open")
        return request_document

    def definition(_anchor, *, deadline: float) -> ProviderLocations:
        events.append("provider")
        return ProviderLocations(
            (
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(1, 8), LspPosition(1, 14)),
                ),
            ),
            "provider_reported",
            False,
        )

    def read_target(path: Path, max_bytes: int, **kwargs) -> bytes:
        events.append(f"read:{path.relative_to(repository).as_posix()}")
        return original_read(path, max_bytes, **kwargs)

    monkeypatch.setattr(code_navigation, "compute_revision", compute)
    monkeypatch.setattr(code_navigation, "read_stable_bytes", read_target, raising=False)
    monkeypatch.setattr(session, "synchronize", synchronize)
    monkeypatch.setattr(session, "open_document", open_document)
    monkeypatch.setattr(session, "definition", definition)
    absolute_deadline = time.monotonic() + 5
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=absolute_deadline,
        )
        assert result.status is NavigationStatus.OK
        assert events == [
            "pre",
            "synchronize",
            "read:pkg/service.py",
            "open",
            "provider",
            "structural",
            "read:pkg/api.py",
            "post",
        ]
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_provider_return_after_deadline_prevents_structural_callback(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    clock = {"now": 1.0}
    real_monotonic = time.monotonic
    structural_calls = 0

    def definition(anchor: SourceAnchor, *, deadline: float) -> ProviderLocations:
        clock["now"] = 11.0
        return ProviderLocations((), "provider_reported", False)

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
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(code_navigation.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(session, "definition", definition)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=10.0,
        )
        assert structural_calls == 0
        assert result.status is NavigationStatus.TIMEOUT
        assert result.locations == ()
        assert result.provenance == ()
    finally:
        monkeypatch.setattr(code_navigation.time, "monotonic", real_monotonic)
        session.close(deadline=real_monotonic() + 5)


def _operation_slots(operation: str, resolver, verifier) -> dict:
    """Give the operation under test its callback, and leave the others empty."""
    by_operation = {
        "resolve_symbol": {"resolver": resolver},
        "verify_edge": {"edge": verifier},
    }
    slots = {"resolver": None, "edge": None}
    slots.update(by_operation.get(operation, {}))
    return slots


def _single_location_definition(target):
    """A provider that always answers with one location inside the target file."""
    locations = (
        LspLocation(target.uri, LspRange(LspPosition(0, 6), LspPosition(0, 15))),
    )
    return lambda anchor, *, deadline: ProviderLocations(
        locations, "provider_reported", False
    )


@pytest.mark.parametrize("callback_kind", ["structural", "resolver", "edge"])
def test_callback_return_after_deadline_never_publishes_facts(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    callback_kind: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    clock = {"now": 1.0}
    real_monotonic = time.monotonic

    def callback(*args):
        clock["now"] = 11.0
        if callback_kind == "edge":
            return True
        return (_graph_location("pkg/api.py", 6, 15),)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        **_callback_slots(callback_kind, callback),
    )
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    monkeypatch.setattr(code_navigation.time, "monotonic", lambda: clock["now"])
    try:
        if callback_kind == "structural":
            _patch_empty_definitions(monkeypatch, session, document)
        result = _navigation_operation(callback_kind, navigation, scope, 10.0)
        assert result.status is NavigationStatus.TIMEOUT
        assert result.locations == ()
        assert result.provenance == ()
    finally:
        monkeypatch.setattr(code_navigation.time, "monotonic", real_monotonic)
        session.close(deadline=real_monotonic() + 5)


def _cross_deadline_on_stable_read(monkeypatch, clock: dict, target) -> None:
    original_read = code_navigation.read_stable_bytes

    def read_target(path: Path, max_bytes: int, **kwargs) -> bytes:
        content = original_read(path, max_bytes, **kwargs)
        if Path(path) == target.absolute_path:
            clock["now"] = 11.0
        return content

    monkeypatch.setattr(code_navigation, "read_stable_bytes", read_target)
    return None


def _cross_deadline_on_source_document(monkeypatch, clock: dict, target) -> None:
    del target
    original_from_bytes = SourceDocument.from_bytes

    def from_bytes(cls, path: str, content: bytes) -> SourceDocument:
        value = original_from_bytes(path, content)
        clock["now"] = 11.0
        return value

    monkeypatch.setattr(SourceDocument, "from_bytes", classmethod(from_bytes))
    return None


def _cross_deadline_on_lsp_range(monkeypatch, clock: dict, target) -> None:
    del target
    original_to_byte_range = SourceDocument.to_byte_range

    def to_byte_range(self, value, encoding):
        result = original_to_byte_range(self, value, encoding)
        clock["now"] = 11.0
        return result

    monkeypatch.setattr(SourceDocument, "to_byte_range", to_byte_range)
    return None


def _cross_deadline_on_byte_position(monkeypatch, clock: dict, target):
    """This stage is reached only for a candidate the structural seam supplies."""
    del target
    original_byte_position = code_navigation.byte_position

    def byte_position(document: SourceDocument, byte_offset: int):
        result = original_byte_position(document, byte_offset)
        clock["now"] = 11.0
        return result

    monkeypatch.setattr(code_navigation, "byte_position", byte_position)

    def structural_candidates(request, deadline):
        return (_graph_location("pkg/api.py", 6, 15),)

    return structural_candidates


_DEADLINE_CROSSING_STAGES = {
    "stable_read": _cross_deadline_on_stable_read,
    "source_document": _cross_deadline_on_source_document,
    "lsp_range": _cross_deadline_on_lsp_range,
    "byte_position": _cross_deadline_on_byte_position,
}


@pytest.mark.parametrize(
    "crossing_stage",
    ["stable_read", "source_document", "lsp_range", "byte_position"],
)
def test_expensive_conversion_crossing_deadline_never_publishes_facts(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    crossing_stage: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    clock = {"now": 1.0}
    real_monotonic = time.monotonic
    provider_calls = 0
    structural = _DEADLINE_CROSSING_STAGES[crossing_stage](monkeypatch, clock, target)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    session._position_encoding = PositionEncoding.UTF8
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(
        session, "open_document", lambda path, *, deadline: document
    )

    def definition(anchor: SourceAnchor, *, deadline: float) -> ProviderLocations:
        nonlocal provider_calls
        provider_calls += 1
        return ProviderLocations(
            (
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                ),
            )
            if crossing_stage in {"stable_read", "lsp_range"}
            else (),
            "provider_reported",
            False,
        )

    monkeypatch.setattr(session, "definition", definition)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=10.0,
        )
        assert result.status is NavigationStatus.TIMEOUT
        assert result.locations == ()
        assert result.provenance == ()
        if crossing_stage == "source_document":
            assert provider_calls == 0
    finally:
        monkeypatch.setattr(code_navigation.time, "monotonic", real_monotonic)
        session.close(deadline=real_monotonic() + 5)


@pytest.mark.parametrize("operation", ["query", "resolve_symbol", "verify_edge"])
def test_result_construction_crossing_deadline_never_publishes_facts(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    clock = {"now": 1.0}
    real_monotonic = time.monotonic
    original_post_init = NavigationResult.__post_init__

    def post_init(result: NavigationResult) -> None:
        original_post_init(result)
        if result.locations:
            clock["now"] = 11.0

    def resolver(*args):
        return (_graph_location("pkg/api.py", 6, 15),)

    def verifier(*args):
        return True

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        **_operation_slots(operation, resolver, verifier),
    )
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    monkeypatch.setattr(code_navigation.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(NavigationResult, "__post_init__", post_init)
    try:
        if operation == "query":
            _patch_stable_attempt(monkeypatch, session, revision, document)
            monkeypatch.setattr(
                session, "definition", _single_location_definition(target)
            )
        result = _navigation_operation(operation, navigation, scope, 10.0)
        assert result.status is NavigationStatus.TIMEOUT
        assert result.locations == ()
        assert result.provenance == ()
    finally:
        monkeypatch.setattr(code_navigation.time, "monotonic", real_monotonic)
        session.close(deadline=real_monotonic() + 5)


@pytest.mark.parametrize("mutate_every_attempt", [False, True])
def test_provider_edit_race_retries_whole_attempt_once_and_never_publishes_stale_facts(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    mutate_every_attempt: bool,
) -> None:
    scope = resolve_repository_scope(repository)
    revisions = (_revision(repository, "a"), _revision(repository, "b"), _revision(repository, "c"))
    request_document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    current_revision = 0
    provider_calls = 0
    synchronize_calls = 0

    def compute(_repository: RepositoryScope, *, deadline: float) -> WorkspaceRevision:
        return revisions[current_revision]

    def synchronize(_revision: WorkspaceRevision, *, deadline: float) -> None:
        nonlocal synchronize_calls
        synchronize_calls += 1

    def definition(_anchor, *, deadline: float) -> ProviderLocations:
        nonlocal current_revision, provider_calls
        provider_calls += 1
        if provider_calls == 1 or mutate_every_attempt:
            current_revision += 1
        return ProviderLocations(
            (
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(1, 8), LspPosition(1, 14)),
                ),
            ),
            "provider_reported",
            False,
        )

    navigation, session = _navigation(repository, state_root, semantic_pyright)
    session._position_encoding = PositionEncoding.UTF8
    monkeypatch.setattr(code_navigation, "compute_revision", compute)
    monkeypatch.setattr(session, "synchronize", synchronize)
    monkeypatch.setattr(session, "open_document", lambda path, *, deadline: request_document)
    monkeypatch.setattr(session, "definition", definition)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert provider_calls == 2
        assert synchronize_calls == 2
        if mutate_every_attempt:
            assert result.status is NavigationStatus.STALE
            assert result.locations == ()
            assert result.diagnostics == ()
            assert result.hover is None
            assert result.provenance == ()
        else:
            assert result.status is NavigationStatus.OK
            assert len(result.locations) == 1
            assert result.workspace_revision_before == revisions[1].revision_sha256
            assert result.workspace_revision_after == revisions[1].revision_sha256
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_target_revision_mismatch_aborts_attempt_before_post_revision(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revisions = (_revision(repository, "a"), _revision(repository, "b"))
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    original_read = code_navigation.read_stable_bytes
    revision_calls = 0
    provider_calls = 0
    target_reads = 0

    def compute(_repository: RepositoryScope, *, deadline: float) -> WorkspaceRevision:
        nonlocal revision_calls
        revision_calls += 1
        return revisions[0] if revision_calls == 1 else revisions[1]

    def read_target(path: Path, max_bytes: int, **kwargs) -> bytes:
        nonlocal target_reads
        content = original_read(path, max_bytes, **kwargs)
        if Path(path) == target.absolute_path:
            target_reads += 1
            if target_reads == 1:
                return bytes([content[0] ^ 1]) + content[1:]
        return content

    def definition(anchor: SourceAnchor, *, deadline: float) -> ProviderLocations:
        nonlocal provider_calls
        provider_calls += 1
        return ProviderLocations(
            (
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                ),
            ),
            "provider_reported",
            False,
        )

    navigation, session = _navigation(repository, state_root, semantic_pyright)
    session._position_encoding = PositionEncoding.UTF8
    monkeypatch.setattr(code_navigation, "compute_revision", compute)
    monkeypatch.setattr(code_navigation, "read_stable_bytes", read_target)
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(
        session, "open_document", lambda path, *, deadline: document
    )
    monkeypatch.setattr(session, "definition", definition)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert provider_calls == 2
        assert target_reads == 2
        assert revision_calls == 3
        assert result.status is NavigationStatus.OK
        assert len(result.locations) == 1
        assert result.workspace_revision_before == revisions[1].revision_sha256
        assert result.workspace_revision_after == revisions[1].revision_sha256
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def _fenced_operation_slots(operation: str) -> dict:
    """The structural and edge seams this operation needs, if it needs any."""
    slots = {"structural": None, "edge": None}
    if operation == "structural":
        slots["structural"] = lambda request, deadline: (
            _graph_location("pkg/api.py", 6, 15),
        )
    if operation == "verify_edge":
        slots["edge"] = lambda source, target, repository, deadline: True
    return slots


def _target_locations(operation: str, target) -> tuple:
    """Only the provider case answers with a location of its own."""
    if operation != "provider_target":
        return ()
    return (
        LspLocation(target.uri, LspRange(LspPosition(0, 6), LspPosition(0, 15))),
    )


def _fenced_query_result(monkeypatch, session, navigation, scope, document, operation, target):
    """Run the query path with a provider answer shaped for this operation."""
    session._position_encoding = PositionEncoding.UTF8
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(session, "open_document", lambda path, *, deadline: document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            _target_locations(operation, target), "provider_reported", False
        ),
    )
    return _navigation_operation("query", navigation, scope, time.monotonic() + 5)


def _fenced_result(monkeypatch, session, navigation, scope, document, operation, target):
    if operation in {"provider_target", "structural"}:
        return _fenced_query_result(
            monkeypatch, session, navigation, scope, document, operation, target
        )
    return _navigation_operation(
        "verify_edge", navigation, scope, time.monotonic() + 5
    )


def _expected_fenced_outcome(mutate_every_attempt: bool, operation: str) -> tuple:
    """Status, location count and empty provenance a fenced retry may end with."""
    if mutate_every_attempt:
        return (NavigationStatus.STALE, 0, True)
    if operation == "structural":
        return (NavigationStatus.PARTIAL, 1, False)
    return (NavigationStatus.OK, 1, False)


def _fenced_outcome(result) -> tuple:
    return (result.status, len(result.locations), result.provenance == ())


@pytest.mark.parametrize("operation", ["provider_target", "structural", "verify_edge"])
@pytest.mark.parametrize("failure_type", [FileNotFoundError, PermissionError, OSError])
@pytest.mark.parametrize("mutate_every_attempt", [False, True])
def test_pre_revision_disk_read_failure_retries_every_fenced_path(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure_type: type[OSError],
    mutate_every_attempt: bool,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    original_read = code_navigation.read_stable_bytes
    target_reads = 0

    def read_target(path: Path, max_bytes: int, **kwargs) -> bytes:
        nonlocal target_reads
        if Path(path) == target.absolute_path:
            target_reads += 1
            if target_reads == 1 or mutate_every_attempt:
                raise failure_type("revision target changed")
        return original_read(path, max_bytes, **kwargs)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        **_fenced_operation_slots(operation),
    )
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    monkeypatch.setattr(code_navigation, "read_stable_bytes", read_target)
    try:
        result = _fenced_result(
            monkeypatch, session, navigation, scope, document, operation, target
        )
        assert target_reads == 2
        assert _fenced_outcome(result) == _expected_fenced_outcome(
            mutate_every_attempt, operation
        )
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize("mutate_every_attempt", [False, True])
def test_query_source_hash_is_checked_before_utf8_decode(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    mutate_every_attempt: bool,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    clean = _open_document(scope, "pkg/service.py")
    invalid_content = b"\xff" + clean.content[1:]
    invalid = OpenDocument(
        clean.source,
        invalid_content,
        hashlib.sha256(invalid_content).hexdigest(),
        clean.version,
    )
    open_calls = 0
    provider_calls = 0

    def open_document(path: str, *, deadline: float) -> OpenDocument:
        nonlocal open_calls
        open_calls += 1
        if open_calls == 1 or mutate_every_attempt:
            return invalid
        return clean

    def definition(anchor: SourceAnchor, *, deadline: float) -> ProviderLocations:
        nonlocal provider_calls
        provider_calls += 1
        return ProviderLocations((), "provider_reported", False)

    navigation, session = _navigation(repository, state_root, semantic_pyright)
    session._position_encoding = PositionEncoding.UTF8
    monkeypatch.setattr(
        code_navigation,
        "compute_revision",
        lambda repository, *, deadline: revision,
    )
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(session, "open_document", open_document)
    monkeypatch.setattr(session, "definition", definition)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert open_calls == 2
        if mutate_every_attempt:
            assert provider_calls == 0
            assert result.status is NavigationStatus.STALE
            assert result.locations == ()
        else:
            assert provider_calls == 1
            assert result.status is NavigationStatus.OK
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize(
    "mismatch",
    ["repository_id", "checkout_id", "relative_path", "absolute_path", "uri"],
)
def test_open_document_is_bound_exactly_before_any_fact_callback(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    clean = _open_document(scope, "pkg/service.py")
    other_source = resolve_repository_source(scope, "pkg/api.py")
    source_changes = {
        "repository_id": {"repository_id": "repository:attacker"},
        "checkout_id": {"checkout_id": "checkout:attacker"},
        "relative_path": {"relative_path": "pkg/api.py"},
        "absolute_path": {"absolute_path": other_source.absolute_path},
        "uri": {"uri": other_source.uri},
    }
    malicious = replace(clean, source=replace(clean.source, **source_changes[mismatch]))
    provider_calls = 0
    structural_calls = 0

    def definition(anchor: SourceAnchor, *, deadline: float) -> ProviderLocations:
        nonlocal provider_calls
        provider_calls += 1
        return ProviderLocations((), "provider_reported", False)

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
    _patch_stable_attempt(monkeypatch, session, revision, malicious)
    monkeypatch.setattr(session, "definition", definition)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is NavigationStatus.ERROR
        assert result.effective_capability is None
        assert result.locations == ()
        assert result.provenance == ()
        assert provider_calls == 0
        assert structural_calls == 0
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_invalid_utf8_already_recorded_by_revision_is_error_not_stale(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = repository / "pkg/service.py"
    source_path.write_bytes(b"\xffinvalid\n")
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    provider_calls = 0

    def definition(anchor: SourceAnchor, *, deadline: float) -> ProviderLocations:
        nonlocal provider_calls
        provider_calls += 1
        return ProviderLocations((), "provider_reported", False)

    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(session, "definition", definition)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 1, 0
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is NavigationStatus.ERROR
        assert result.locations == ()
        assert provider_calls == 0
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_query_indexes_revision_entries_once_for_all_targets(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(3):
        (repository / f"pkg/target_{index}.py").write_text(
            f"value_{index} = {index}\n", encoding="utf-8"
        )
    scope = resolve_repository_scope(repository)
    base_revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    targets = tuple(
        resolve_repository_source(scope, f"pkg/target_{index}.py")
        for index in range(3)
    )

    class CountingEntries:
        def __init__(self, values) -> None:
            self.values = values
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            return iter(self.values)

    entries = CountingEntries(base_revision.entries)
    revision = replace(base_revision, entries=entries)  # type: ignore[arg-type]
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            tuple(
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(0, 0), LspPosition(0, 7)),
                )
                for target in targets
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
        assert result.status is NavigationStatus.OK
        assert len(result.locations) == 3
        assert entries.iterations == 1
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_target_read_timeout_aborts_without_partial_publication(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    original_read = code_navigation.read_stable_bytes
    revision_calls = 0

    def compute(_repository: RepositoryScope, *, deadline: float) -> WorkspaceRevision:
        nonlocal revision_calls
        revision_calls += 1
        return revision

    def read_target(path: Path, max_bytes: int, **kwargs) -> bytes:
        if Path(path) == target.absolute_path:
            raise TimeoutError("D:/private/target-read.py")
        return original_read(path, max_bytes, **kwargs)

    navigation, session = _navigation(repository, state_root, semantic_pyright)
    session._position_encoding = PositionEncoding.UTF8
    monkeypatch.setattr(code_navigation, "compute_revision", compute)
    monkeypatch.setattr(code_navigation, "read_stable_bytes", read_target)
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(
        session, "open_document", lambda path, *, deadline: document
    )
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
            deadline=time.monotonic() + 5,
        )
        assert revision_calls == 1
        assert result.status is NavigationStatus.TIMEOUT
        assert result.locations == ()
        assert result.diagnostics == ()
        assert result.provenance == ()
        assert all("private" not in warning for warning in result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize(
    ("coverage", "partial", "expected"),
    [
        ("provider_reported", False, NavigationStatus.OK),
        ("provider_reported", True, NavigationStatus.PARTIAL),
        ("unsupported", True, NavigationStatus.UNSUPPORTED),
        ("not_ready", True, NavigationStatus.NOT_READY),
    ],
)
def test_provider_location_coverage_and_partial_are_preserved(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    coverage: str,
    partial: bool,
    expected: NavigationStatus,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations((), coverage, partial),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is expected
        assert result.locations == ()
        assert result.resolution is (
            ResolutionLabel.UNSUPPORTED
            if expected is NavigationStatus.UNSUPPORTED
            else ResolutionLabel.UNRESOLVED
        )
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize(
    ("coverage", "expected"),
    [
        ("unsupported", NavigationStatus.UNSUPPORTED),
        ("not_ready", NavigationStatus.NOT_READY),
    ],
)
def test_provider_call_coverage_is_not_stripped(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    coverage: str,
    expected: NavigationStatus,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "incoming_calls",
        lambda anchor, *, deadline: ProviderCalls("incoming", (), coverage, True),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope,
                Capability.CALLS,
                "pkg/service.py",
                10,
                20,
                direction="incoming",
            ),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is expected
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_calls_fallback_classifies_only_structurally_correlated_references(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    reference_calls = 0

    def references(anchor: SourceAnchor, *, deadline: float) -> ProviderLocations:
        nonlocal reference_calls
        reference_calls += 1
        return ProviderLocations(
            (
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                ),
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(0, 0), LspPosition(0, 5)),
                ),
            ),
            "provider_reported",
            False,
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
    monkeypatch.setattr(
        session,
        "incoming_calls",
        lambda anchor, *, deadline: ProviderCalls(
            "incoming", (), "unsupported", True
        ),
    )
    monkeypatch.setattr(session, "references", references)
    try:
        result = navigation.query(
            NavigationRequest(
                scope,
                Capability.CALLS,
                "pkg/service.py",
                10,
                20,
                direction="incoming",
            ),
            deadline=time.monotonic() + 5,
        )
        assert reference_calls == 1
        assert result.status is NavigationStatus.PARTIAL
        assert result.effective_capability is Capability.REFERENCES
        assert len(result.locations) == 1
        assert result.locations[0].range == PositionRange(6, 15)
        assert result.locations[0].resolution is ResolutionLabel.LSP_AND_GRAPH
        assert {item.provider for item in result.locations[0].provenance} == {
            "pyright",
            "evidence-graph",
        }
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_types_combines_type_definition_and_hover_partial(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "type_definition",
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
    monkeypatch.setattr(
        session,
        "hover",
        lambda anchor, *, deadline: ProviderHover("type hover", None, True),
    )
    try:
        result = navigation.query(
            NavigationRequest(scope, Capability.TYPES, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.effective_capability is Capability.TYPES
        assert result.hover == "type hover"
        assert len(result.locations) == 1
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_types_marks_invalid_hover_range_partial_without_dropping_contents(
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
        "type_definition",
        lambda anchor, *, deadline: ProviderLocations(
            (), "provider_reported", False
        ),
    )
    monkeypatch.setattr(
        session,
        "hover",
        lambda anchor, *, deadline: ProviderHover(
            "type hover",
            LspRange(LspPosition(999, 0), LspPosition(999, 1)),
            False,
        ),
    )
    try:
        result = navigation.query(
            NavigationRequest(scope, Capability.TYPES, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.hover == "type hover"
        assert "hover" in " ".join(result.warnings)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize(
    "unavailable_part",
    ["type_definition_error", "type_definition_unsupported", "hover_error"],
)
def test_types_subrequests_are_independent(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    unavailable_part: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    calls: list[str] = []

    def type_definition(
        anchor: SourceAnchor, *, deadline: float
    ) -> ProviderLocations:
        calls.append("type_definition")
        if unavailable_part == "type_definition_error":
            raise RuntimeError("D:/private/type-definition.py")
        if unavailable_part == "type_definition_unsupported":
            return ProviderLocations((), "unsupported", True)
        return ProviderLocations(
            (
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                ),
            ),
            "provider_reported",
            False,
        )

    def hover(anchor: SourceAnchor, *, deadline: float) -> ProviderHover:
        calls.append("hover")
        if unavailable_part == "hover_error":
            raise RuntimeError("D:/private/hover.py")
        return ProviderHover("type hover", None, False)

    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(session, "type_definition", type_definition)
    monkeypatch.setattr(session, "hover", hover)
    try:
        result = navigation.query(
            NavigationRequest(scope, Capability.TYPES, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert calls == ["type_definition", "hover"]
        assert result.status is NavigationStatus.PARTIAL
        assert result.effective_capability is Capability.TYPES
        if unavailable_part == "hover_error":
            assert len(result.locations) == 1
            assert result.hover is None
        else:
            assert result.locations == ()
            assert result.hover == "type hover"
        assert all("private" not in warning for warning in result.warnings)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)
