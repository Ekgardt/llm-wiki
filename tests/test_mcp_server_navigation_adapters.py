"""Navigation adapters return only contained, hash-bound, redacted and bounded evidence."""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
from lsp_profiles import PYRIGHT_PROFILE

from tests.mcp_server_support import (
    _assert_absent_span_hash_is_refused_only_when_required,
    _assert_bounded_source_reads,
    _assert_manager_request,
    _assert_mismatched_hashes_are_refused,
    _assert_navigation_constructor,
    _assert_navigation_text_is_redacted,
    _assert_normalized_error_data,
    _assert_normalized_timeout_data,
    _assert_precise_request_window,
    _assert_precise_route_data,
    _assert_quality_without_mutation,
    _assert_read_only_graph_options,
    _assert_relative_graph_locations,
    _assert_rendered_code_is_redacted,
    _assert_rendered_fields_survive_redaction,
    _assert_single_render_window,
    _assert_static_timeout_envelope,
    _assert_text_is_redacted,
    _assert_the_cache_holds_the_newest_source,
    _assert_timeout_envelope_data,
    _assert_timeout_warning_only,
    _navigation_location_result,
    _replace_in_every_server_part,
)
from tests.slow_machine import SHORT_TIMEOUT


@pytest.mark.parametrize(
    ("mode", "capability_name", "direction"),
    [
        ("definition", "DEFINITIONS", None),
        ("references", "REFERENCES", None),
        ("implementations", "IMPLEMENTATIONS", None),
        ("type", "TYPES", None),
        ("diagnostics", "DIAGNOSTICS", None),
        ("callers", "CALLS", "incoming"),
        ("callees", "CALLS", "outgoing"),
    ],
)
def test_every_precise_route_builds_one_request_and_one_renderer_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    capability_name: str,
    direction: str | None,
) -> None:
    import code_navigation
    import code_navigation_renderer
    import mcp_server

    source = tmp_path / "api.py"
    source.write_text("def api():\n    return 1\n", encoding="utf-8")
    identity = object()
    query_result = object()
    captures: dict[str, object] = {"requests": [], "renders": []}

    class Session:
        profile = PYRIGHT_PROFILE
        degradation_codes = ()

        @property
        def identity(self):
            return identity

    class Manager:
        def get(self, scope, *, deadline, profile=PYRIGHT_PROFILE):
            captures["manager"] = (scope, deadline)
            assert profile is PYRIGHT_PROFILE
            return Session()

    class Navigation:
        def __init__(self, scope, session, received_identity, **callbacks):
            captures["constructor"] = (
                scope,
                session,
                received_identity,
                callbacks,
            )

        def query(self, request, *, deadline):
            captures["requests"].append((request, deadline))
            return query_result

    def render(result):
        captures["renders"].append(result)
        return {"status": "ok", "warnings": (), "provenance": []}

    manager = Manager()
    manager_requests: list[tuple[float, int, object]] = []

    def manager_factory(deadline, expected_epoch, cancelled):
        manager_requests.append((deadline, expected_epoch, cancelled))
        return manager

    monkeypatch.setattr(code_navigation, "CodeNavigation", Navigation)
    monkeypatch.setattr(code_navigation_renderer, "render_navigation", render)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.navigation_session_manager", manager_factory)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_EPOCH", 73, raising=False)
    deadline = time.monotonic() + 5

    data = mcp_server.get_precise_architecture(
        str(tmp_path),
        mode=mode,
        path="api.py",
        line=1,
        character=4,
        offset=7,
        limit=8,
        deadline=deadline,
    )

    request, query_deadline = captures["requests"][0]
    constructor = captures["constructor"]
    _assert_precise_request_window(request, capability_name, direction)
    assert query_deadline == deadline
    _assert_manager_request(manager_requests, deadline)
    assert captures["manager"][1] == deadline
    _assert_navigation_constructor(constructor, identity)
    _assert_single_render_window(captures, query_result)
    _assert_precise_route_data(data, mode)


def test_real_navigation_adapters_return_only_contained_exact_graph_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lsp_security
    import mcp_server
    from code_intelligence import Capability
    from code_navigation import NavigationRequest
    from lsp_positions import SourceAnchor
    from repository_scope import resolve_repository_scope

    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    content = (
        b"def caller():\n"
        b"    callee()\n"
        b"\n"
        b"def callee():\n"
        b"    pass\n"
    )
    source.write_bytes(content)
    scope = resolve_repository_scope(tmp_path)
    caller_start = content.index(b"caller")
    call_start = content.index(b"callee")
    declaration_start = content.rindex(b"callee")
    source_hash = hashlib.sha256(content).hexdigest()
    reads: list[tuple[str, int, float | None]] = []
    original_read = lsp_security.read_repository_source_bytes

    class Graph:
        repository_scope = scope
        generation_id = "generation-17"

        def find_nodes(self, *, name, max_rows, deadline, **_options):
            assert max_rows == 10_000
            node_id = {"caller": "caller-node", "callee": "callee-node"}.get(name)
            return [] if node_id is None else [
                {
                    "node_id": node_id,
                    "kind": "function",
                    "identity_key": name,
                    "metadata": {"name": name},
                }
            ]

        def occurrences(self, node_id, *, max_rows, deadline):
            assert max_rows <= 10_000
            starts = {
                "caller-node": (caller_start, caller_start + len(b"caller"), 1),
                "callee-node": (
                    declaration_start,
                    declaration_start + len(b"callee"),
                    4,
                ),
            }
            if node_id not in starts:
                return []
            start, end, line = starts[node_id]
            return [
                {
                    "file": str(source),
                    "role": "definition",
                        "byte_start": start,
                        "byte_end": end,
                        "line_start": line,
                        "source_sha256": source_hash,
                    }
                ]

        def edges(self, *, edge_types, max_rows, deadline, source_node_ids=None, target_node_ids=None):
            # As EvidenceGraph.edges: the read is anchored on the symbol's nodes, and an
            # anchor that names no node selects nothing.
            assert (edge_types, max_rows) == (("CALLS",), 10_000)
            anchored = {*(source_node_ids or ()), *(target_node_ids or ())}
            if not anchored & {"caller-node", "callee-node"}:
                return []
            return [
                {
                    "assertion_id": "call-edge",
                    "source_node_id": "caller-node",
                    "target_node_id": "callee-node",
                }
            ]

        def evidence_spans(self, *, assertion_id, max_rows, deadline):
            assert assertion_id == "call-edge"
            return [
                {
                    "relative_path": "src/app.py",
                    "byte_start": call_start,
                    "byte_end": call_start + len(b"callee"),
                    "line_start": 2,
                    "source_sha256": source_hash,
                    "span_sha256": hashlib.sha256(b"callee").hexdigest(),
                }
            ][:max_rows]

        def close(self):
            return None

    def stable_read(repository, relative_path, *, max_bytes, deadline=None):
        reads.append((relative_path, max_bytes, deadline))
        return original_read(
            repository,
            relative_path,
            max_bytes=max_bytes,
            deadline=deadline,
        )

    monkeypatch.setattr(
        "llm_wiki.code_graph_generation.active_evidence_graph", lambda _root, **_options: Graph()
    )
    monkeypatch.setattr(lsp_security, "read_repository_source_bytes", stable_read)
    monkeypatch.setattr(
        "llm_wiki.code_graph_parse.index_directory",
        lambda *_args, **_kwargs: pytest.fail("adapter must never index"),
    )
    deadline = time.monotonic() + 5
    definition_request = NavigationRequest(
        scope,
        Capability.DEFINITIONS,
        "src/app.py",
        2,
        6,
    )
    calls_request = NavigationRequest(
        scope,
        Capability.CALLS,
        "src/app.py",
        1,
        5,
        direction="outgoing",
    )

    definitions = mcp_server.navigation_structural_candidates(
        definition_request,
        deadline,
    )
    calls = mcp_server.navigation_structural_candidates(calls_request, deadline)
    resolved = mcp_server.navigation_symbol_resolver("callee", scope, deadline)
    verified = mcp_server.navigation_edge_verifier(
        SourceAnchor("src/app.py", 1, 4, caller_start),
        SourceAnchor("src/app.py", 4, 4, declaration_start),
        scope,
        deadline,
    )

    assert [(item.path, item.range.byte_start, item.range.byte_end) for item in definitions] == [
        ("src/app.py", declaration_start, declaration_start + len(b"callee"))
    ]
    assert [(item.path, item.range.byte_start, item.range.byte_end) for item in calls] == [
        ("src/app.py", call_start, call_start + len(b"callee"))
    ]
    assert resolved == definitions
    assert verified is True
    _assert_relative_graph_locations((*definitions, *calls))
    _assert_bounded_source_reads(reads, deadline)


def test_navigation_source_bytes_uses_retained_containment_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lsp_security
    import mcp_server
    from repository_scope import resolve_repository_scope

    scope = resolve_repository_scope(tmp_path)
    captured = {}
    deadline = time.monotonic() + 5

    def read_source(repository, relative_path, *, max_bytes, deadline):
        captured.update(
            repository=repository,
            relative_path=relative_path,
            max_bytes=max_bytes,
            deadline=deadline,
        )
        return b"value = 1\n"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("navigation source reopened a pathname")

    monkeypatch.setattr(lsp_security, "read_repository_source_bytes", read_source)
    monkeypatch.setattr(lsp_security, "resolve_repository_source", forbidden)
    _replace_in_every_server_part(monkeypatch, "read_stable_bytes", forbidden)

    assert mcp_server.navigation_source_bytes(
        scope,
        "source.py",
        deadline=deadline,
    ) == b"value = 1\n"
    assert captured == {
        "repository": scope,
        "relative_path": "source.py",
        "max_bytes": mcp_server.MAX_NAVIGATION_SOURCE_BYTES,
        "deadline": deadline,
    }


def test_navigation_location_requires_matching_source_and_evidence_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from repository_scope import resolve_repository_scope

    content = b"def api():\n    return 1\n"
    scope = resolve_repository_scope(tmp_path)
    source_hash = hashlib.sha256(content).hexdigest()
    start = content.index(b"api")
    span = {
        "relative_path": "api.py",
        "role": "definition",
        "byte_start": start,
        "byte_end": start + len(b"api"),
        "source_sha256": source_hash,
        "span_sha256": hashlib.sha256(b"api").hexdigest(),
    }
    monkeypatch.setattr(
        "llm_wiki.mcp_server_navigation.navigation_source_bytes",
        lambda *_args, **_kwargs: content,
    )

    assert _navigation_location_result(scope, span) is not None
    _assert_mismatched_hashes_are_refused(scope, span)
    _assert_absent_span_hash_is_refused_only_when_required(scope, span)


def test_navigation_graph_callback_reads_each_hash_bound_source_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server
    from repository_scope import resolve_repository_scope

    content = b"def alpha(): pass\ndef beta(): pass\n"
    source_hash = hashlib.sha256(content).hexdigest()
    scope = resolve_repository_scope(tmp_path)
    reads = 0

    class Graph:
        repository_scope = scope
        generation_id = "generation-1"

        def find_nodes(self, **_kwargs):
            return [{"node_id": "node", "metadata": {"name": "alpha"}}]

        def occurrences(self, *_args, **_kwargs):
            return [
                {
                    "relative_path": "api.py",
                    "role": "definition",
                    "byte_start": content.index(name),
                    "byte_end": content.index(name) + len(name),
                    "source_sha256": source_hash,
                }
                for name in (b"alpha", b"beta")
            ]

        def close(self):
            return None

    def read_source(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        return content

    monkeypatch.setattr(
        "llm_wiki.code_graph_generation.active_evidence_graph",
        lambda _root, **_options: Graph(),
    )
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.navigation_source_bytes", read_source)

    locations = mcp_server.graph_declaration_locations(
        "alpha",
        scope,
        time.monotonic() + 5,
    )

    assert len(locations) == 2
    assert reads == 1
    assert mcp_server.MAX_NAVIGATION_GRAPH_FACTS == 10_000
    assert mcp_server.MAX_NAVIGATION_SOURCE_CACHE_BYTES == 64 * 1024 * 1024


def test_structural_callback_reuses_anchor_source_for_graph_spans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server
    from code_intelligence import Capability
    from code_navigation import NavigationRequest
    from repository_scope import resolve_repository_scope

    content = b"def alpha(): pass\n"
    scope = resolve_repository_scope(tmp_path)
    source_hash = hashlib.sha256(content).hexdigest()
    reads = 0

    class Graph:
        repository_scope = scope
        generation_id = "generation-1"

        def find_nodes(self, **_kwargs):
            return [{"node_id": "alpha", "metadata": {"name": "alpha"}}]

        def occurrences(self, *_args, **_kwargs):
            return [
                {
                    "relative_path": "api.py",
                    "role": "definition",
                    "byte_start": 4,
                    "byte_end": 9,
                    "line_start": 1,
                    "source_sha256": source_hash,
                }
            ]

        def close(self):
            return None

    def read_source(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        return content

    monkeypatch.setattr(
        "llm_wiki.code_graph_generation.active_evidence_graph",
        lambda _root, **_options: Graph(),
    )
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.navigation_source_bytes", read_source)
    request = NavigationRequest(
        scope,
        Capability.DEFINITIONS,
        "api.py",
        1,
        4,
    )

    assert len(
        mcp_server.navigation_structural_candidates(
            request,
            time.monotonic() + 5,
        )
    ) == 1
    assert reads == 1


def test_navigation_source_cache_evicts_instead_of_refusing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server
    from repository_scope import resolve_repository_scope

    scope = resolve_repository_scope(tmp_path)
    reads = 0

    def read_source(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        return b"12345"

    monkeypatch.setattr("llm_wiki.mcp_server_limits.MAX_NAVIGATION_SOURCE_CACHE_BYTES", 4)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.navigation_source_bytes", read_source)
    cache = mcp_server.NavigationSourceCache()

    deadline = time.monotonic() + SHORT_TIMEOUT
    assert cache.read(scope, "small.py", deadline=deadline)[0] == b"12345"
    assert cache.read(scope, "large.py", deadline=deadline)[0] == b"12345"
    assert cache.read(scope, "large.py", deadline=deadline)[0] == b"12345"
    _assert_the_cache_holds_the_newest_source(cache, reads)


def test_navigation_calls_use_lightweight_evidence_spans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server
    from repository_scope import resolve_repository_scope

    content = b"callee()\n"
    scope = resolve_repository_scope(tmp_path)
    span_calls = 0
    include_span_hash = True

    class Graph:
        repository_scope = scope
        generation_id = "generation-1"

        def find_nodes(self, **_kwargs):
            return [{"node_id": "callee", "metadata": {"name": "callee"}}]

        def edges(self, **_kwargs):
            return [
                {
                    "assertion_id": "call",
                    "source_node_id": "caller",
                    "target_node_id": "callee",
                }
            ]

        def evidence_spans(self, **_kwargs):
            nonlocal span_calls, include_span_hash
            span_calls += 1
            span = {
                "relative_path": "api.py",
                "byte_start": 0,
                "byte_end": len(b"callee"),
                "source_sha256": hashlib.sha256(content).hexdigest(),
            }
            if include_span_hash:
                span["span_sha256"] = hashlib.sha256(b"callee").hexdigest()
            return [span]

        def evidence(self, **_kwargs):
            raise AssertionError("navigation loaded source blobs from the graph")

        def close(self):
            return None

    monkeypatch.setattr(
        "llm_wiki.code_graph_generation.active_evidence_graph",
        lambda _root, **_options: Graph(),
    )
    monkeypatch.setattr(
        "llm_wiki.mcp_server_navigation.navigation_source_bytes",
        lambda *_args, **_kwargs: content,
    )

    locations = mcp_server.graph_call_locations(
        "callee",
        scope,
        direction="incoming",
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )
    include_span_hash = False
    missing_hash_locations = mcp_server.graph_call_locations(
        "callee",
        scope,
        direction="incoming",
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )

    assert len(locations) == 1
    assert missing_hash_locations == ()
    assert span_calls == 2


def test_navigation_graph_open_is_read_only_and_deadline_aware(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server
    from repository_scope import resolve_repository_scope

    scope = resolve_repository_scope(tmp_path)
    captured = {}

    class Graph:
        repository_scope = scope

    graph = Graph()

    def open_graph(root, **options):
        captured.update(root=root, options=options)
        return graph

    monkeypatch.setattr("llm_wiki.code_graph_generation.active_evidence_graph", open_graph)
    deadline = time.monotonic() + 5

    assert mcp_server.open_navigation_graph(scope, deadline) is graph
    _assert_read_only_graph_options(captured, scope, deadline)


def test_navigation_adapter_returns_empty_when_exact_graph_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server
    from code_intelligence import Capability
    from code_navigation import NavigationRequest
    from repository_scope import resolve_repository_scope

    source = tmp_path / "api.py"
    source.write_text("def api():\n    pass\n", encoding="utf-8")
    scope = resolve_repository_scope(tmp_path)
    monkeypatch.setattr(
        "llm_wiki.code_graph_generation.active_evidence_graph", lambda _root, **_options: None
    )
    request = NavigationRequest(
        scope,
        Capability.IMPLEMENTATIONS,
        "api.py",
        1,
        4,
    )

    assert mcp_server.navigation_structural_candidates(
        request,
        time.monotonic() + 5,
    ) == ()


def test_navigation_adapter_checks_deadline_after_graph_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server
    from repository_scope import resolve_repository_scope

    scope = resolve_repository_scope(tmp_path)
    now = [100.0]

    class Clock:
        @staticmethod
        def monotonic():
            return now[0]

    class Graph:
        repository_scope = scope
        generation_id = "generation-17"

        def find_nodes(self, **_kwargs):
            now[0] = 101.0
            return []

        def close(self):
            return None

    _replace_in_every_server_part(monkeypatch, "time", Clock)
    monkeypatch.setattr(
        "llm_wiki.code_graph_generation.active_evidence_graph", lambda _root, **_options: Graph()
    )

    with pytest.raises(TimeoutError):
        mcp_server.navigation_symbol_resolver("api", scope, 100.5)


def test_navigation_graph_evidence_work_is_globally_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server
    from lsp_positions import SourceAnchor
    from repository_scope import resolve_repository_scope

    scope = resolve_repository_scope(tmp_path)
    span_attempts = 0

    class Graph:
        generation_id = "generation-17"

        def find_nodes(self, **_kwargs):
            return [
                {"node_id": f"source-{index}", "metadata": {"name": "source"}}
                for index in range(3)
            ]

        def occurrences(self, *_args, **_kwargs):
            return [
                {"role": "definition", "invalid": index}
                for index in range(3)
            ]

    def location(*_args, **_kwargs):
        nonlocal span_attempts
        span_attempts += 1
        return None

    monkeypatch.setattr("llm_wiki.mcp_server_limits.MAX_NAVIGATION_GRAPH_FACTS", 3)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.navigation_location_from_span", location)

    assert mcp_server.graph_node_ids_at_anchor(
        Graph(),
        "source",
        SourceAnchor("source.py", 1, 0, 0),
        scope,
        time.monotonic() + 5,
    ) == set()
    assert span_attempts <= 3


def test_precise_dispatch_exception_returns_normalized_redacted_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server

    sensitive = (
        r"api_key=sk-abcdefghijklmnopqrstuvwxyz "
        r"C:\Users\operator\outside\secret.py"
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError(sensitive)

    monkeypatch.setattr("llm_wiki.mcp_server_navigation.get_precise_architecture", fail)
    text = asyncio.run(
        mcp_server.handle_tool_call(
            "get_architecture",
            {
                "directory": str(tmp_path),
                "mode": "definition",
                "path": "api.py",
                "line": 1,
                "character": 0,
            },
        )
    )
    envelope = json.loads(text)

    _assert_normalized_error_data(envelope["data"])
    _assert_text_is_redacted(text, sensitive)


def test_renderer_value_error_maps_to_normalized_navigation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code_navigation
    import code_navigation_renderer
    import mcp_server

    source = tmp_path / "api.py"
    source.write_text("def api():\n    pass\n", encoding="utf-8")

    class Session:
        identity = object()
        profile = PYRIGHT_PROFILE
        degradation_codes = ()

    class Manager:
        def get(self, *_args, **_kwargs):
            return Session()

    class Navigation:
        def __init__(self, *_args, **_kwargs):
            pass

        def query(self, *_args, **_kwargs):
            return object()

    monkeypatch.setattr(code_navigation, "CodeNavigation", Navigation)
    monkeypatch.setattr(
        code_navigation_renderer,
        "render_navigation",
        lambda _result: (_ for _ in ()).throw(ValueError("sensitive renderer detail")),
    )
    monkeypatch.setattr(
        "llm_wiki.mcp_server_navigation.navigation_session_manager",
        lambda _deadline, _expected_epoch, _cancelled: Manager(),
    )

    data = mcp_server.get_precise_architecture(
        str(tmp_path),
        mode="definition",
        path="api.py",
        line=1,
        character=4,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )

    assert data["status"] == "error"
    assert data["warnings"] == ("navigation_render_failed",)
    assert "error" not in data


def test_hard_precise_timeout_returns_normalized_timeout_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    import mcp_server

    release = threading.Event()

    def blocked(*_args, **_kwargs):
        release.wait(1)
        return {"status": "ok"}

    monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_LSP_STARTUP_SECONDS", 0.05)
    monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_WORKERS", set())
    monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_WORKERS_LOCK", threading.Lock())
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.get_precise_architecture", blocked)
    try:
        text = asyncio.run(
            mcp_server.handle_tool_call(
                "get_architecture",
                {
                    "directory": str(tmp_path),
                    "mode": "references",
                    "path": "api.py",
                    "line": 1,
                    "character": 0,
                },
            )
        )
    finally:
        release.set()
    data = json.loads(text)["data"]

    _assert_normalized_timeout_data(data)
    _assert_timeout_warning_only(data)


def test_precise_timeout_envelope_is_static_and_probe_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server

    def forbidden(*_args, **_kwargs):
        raise AssertionError("timeout formatting invoked a normal response probe")

    monkeypatch.setattr("llm_wiki.mcp_server_quality.build_operation_envelope", forbidden)
    monkeypatch.setattr("llm_wiki.mcp_server_quality.quality_for", forbidden)
    monkeypatch.setattr("llm_wiki.mcp_server_freshness.components_for", forbidden)
    monkeypatch.setattr("llm_wiki.mcp_server_sanitize.sanitize_navigation_data", forbidden)

    envelope = json.loads(
        mcp_server.tool_timeout_envelope_text(
            "get_architecture",
            {
                "directory": r"C:\private\repository",
                "mode": "definition",
                "path": "api.py",
                "line": 1,
                "character": 0,
            },
        )
    )

    _assert_static_timeout_envelope(envelope)
    _assert_timeout_envelope_data(envelope)


@pytest.mark.parametrize(
    ("status", "expected_partial"),
    [
        ("ok", False),
        ("partial", True),
        ("unsupported", True),
        ("not_ready", True),
        ("stale", True),
        ("timeout", True),
        ("error", True),
    ],
)
def test_precise_quality_uses_rendered_status_without_mutating_data(
    status: str,
    expected_partial: bool,
) -> None:
    import mcp_server

    data = mcp_server.normalized_navigation_failure(
        directory="C:/repo",
        mode="definition",
        status=status,
        warning="navigation_warning" if expected_partial else "",
        offset=0,
        limit=10,
    )
    if not expected_partial:
        data["warnings"] = ()
    before = dict(data)

    quality = mcp_server.quality_for(
        "get_architecture",
        data,
        {"mode": "definition"},
    )

    _assert_quality_without_mutation(quality, data, before, expected_partial)


def test_rendered_partial_status_drives_outer_envelope_without_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server

    data = mcp_server.normalized_navigation_failure(
        directory=str(tmp_path),
        mode="definition",
        status="partial",
        warning="output_token_bound",
        offset=0,
        limit=10,
    )
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.get_precise_architecture", lambda *_a, **_k: data)

    envelope = json.loads(
        asyncio.run(
            mcp_server.handle_tool_call(
                "get_architecture",
                {
                    "directory": str(tmp_path),
                    "mode": "definition",
                    "path": "api.py",
                    "line": 1,
                    "character": 0,
                },
            )
        )
    )

    assert envelope["partial"] is True
    assert envelope["data"]["status"] == "partial"
    assert "_navigation_partial" not in envelope["data"]
    assert "output_token_bound" in envelope["warnings"]


def test_navigation_text_fields_are_secret_and_external_path_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server

    sensitive = (
        r"api_key=sk-abcdefghijklmnopqrstuvwxyz "
        r"C:\Users\operator\outside\secret.py"
    )
    data = mcp_server.normalized_navigation_failure(
        directory=str(tmp_path),
        mode="diagnostics",
        status="partial",
        warning=sensitive,
        offset=0,
        limit=10,
    )
    data["hover"] = sensitive
    data["groups"] = [
        {
            "path": "api.py",
            "containing_symbol": None,
            "locations": [
                {
                    "path": "api.py",
                    "line": 1,
                    "character": 0,
                    "range": {"byte_start": 0, "byte_end": 3},
                    "containing_symbol": None,
                    "signature": sensitive,
                    "resolution": "lsp_confirmed",
                }
            ],
        }
    ]
    data["diagnostics"] = [
        {
            "path": "api.py",
            "range": {"byte_start": 0, "byte_end": 3},
            "severity": "error",
            "code": sensitive,
            "message": sensitive,
            "related": [data["groups"][0]["locations"][0]],
        }
    ]
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.get_precise_architecture", lambda *_a, **_k: data)

    text = asyncio.run(
        mcp_server.handle_tool_call(
            "get_architecture",
            {
                "directory": str(tmp_path),
                "mode": "diagnostics",
                "path": "api.py",
                "line": 1,
                "character": 0,
            },
        )
    )
    rendered = json.loads(text)["data"]

    external_path = r"C:\Users\operator\outside\secret.py"
    _assert_navigation_text_is_redacted(text, external_path)
    _assert_rendered_fields_survive_redaction(rendered)
    _assert_rendered_code_is_redacted(rendered, external_path)
    assert rendered["hover"]
    assert rendered["groups"][0]["path"] == "api.py"


def test_navigation_text_sanitizer_never_slices_a_redaction_marker() -> None:
    import mcp_server

    value = "x /a/b"
    sanitized = mcp_server.sanitize_navigation_text(value)

    assert sanitized == "x "
    assert len(sanitized.encode("utf-8")) <= len(value.encode("utf-8"))
    assert "[PAT" not in sanitized


def test_precise_components_report_provider_and_graph_without_private_state() -> None:
    import mcp_server

    data = mcp_server.normalized_navigation_failure(
        directory="C:/repo",
        mode="definition",
        status="ok",
        warning="",
        offset=0,
        limit=10,
        provider="pyright",
        provider_version="1.1.411",
        readiness="query_ready",
    )
    data["warnings"] = ()
    data["provenance"] = [
        {
            "source": "graph",
            "provider": "evidence-graph",
            "version": "generation-17",
            "observation": "graph_candidate",
        }
    ]

    assert mcp_server.components_for("get_architecture", data) == {
        "provider": {"generation": "1.1.411", "freshness": "fresh"},
        "graph": {"generation": "generation-17", "freshness": "fresh"},
    }
