"""Precise architecture answers through the managed language-server session."""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

from llm_wiki import (
    mcp_server_limits,
    mcp_server_sanitize,
    mcp_server_validation,
    mcp_server_workers,
    runtime_roots,
)
from llm_wiki.mcp_server_limits import (
    MAX_NAVIGATION_SOURCE_BYTES,
)
from llm_wiki.mcp_server_validation import (
    _is_integer_value,
)
from llm_wiki.mcp_server_workers import (
    check_deadline,
)

NAVIGATION_MANAGER: object | None = None
NAVIGATION_MANAGER_CLOSING: object | None = None
NAVIGATION_MANAGER_EPOCH = 0
NAVIGATION_MANAGER_LOCK = threading.Lock()

_PRECISE_MODE_CAPABILITY = {
    "definition": "DEFINITIONS",
    "references": "REFERENCES",
    "implementations": "IMPLEMENTATIONS",
    "type": "TYPES",
    "diagnostics": "DIAGNOSTICS",
}


def _acquire_navigation_manager_lock(deadline: float) -> None:
    check_deadline(deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not NAVIGATION_MANAGER_LOCK.acquire(timeout=remaining):
        raise TimeoutError("navigation session manager lock deadline expired")
    try:
        check_deadline(deadline)
    except BaseException:
        NAVIGATION_MANAGER_LOCK.release()
        raise


def _check_navigation_manager_stop(deadline: float, cancelled) -> None:
    check_deadline(deadline)
    if cancelled():
        raise TimeoutError("navigation operation cancelled")
    check_deadline(deadline)


def _require_navigation_manager_lifecycle(expected_epoch: int) -> None:
    """The manager about to be handed out must still be the one asked for."""
    if expected_epoch != NAVIGATION_MANAGER_EPOCH:
        raise TimeoutError("navigation session manager lifecycle changed")
    if NAVIGATION_MANAGER_CLOSING is not None:
        raise TimeoutError("navigation session manager is closing")


def navigation_session_manager(
    deadline: float,
    expected_epoch: int,
    cancelled,
):
    global NAVIGATION_MANAGER
    _check_navigation_manager_stop(deadline, cancelled)
    _acquire_navigation_manager_lock(deadline)
    try:
        _check_navigation_manager_stop(deadline, cancelled)
        _require_navigation_manager_lifecycle(expected_epoch)
        if NAVIGATION_MANAGER is None:
            from llm_wiki.pyright_session_manager import PyrightSessionManager

            _check_navigation_manager_stop(deadline, cancelled)
            manager = PyrightSessionManager(state_root=runtime_roots.state_root())
            _check_navigation_manager_stop(deadline, cancelled)
            NAVIGATION_MANAGER = manager
        _check_navigation_manager_stop(deadline, cancelled)
        return NAVIGATION_MANAGER
    finally:
        NAVIGATION_MANAGER_LOCK.release()


def _detach_navigation_manager():
    """Claim the manager that must be closed. Caller holds the manager lock."""
    global NAVIGATION_MANAGER, NAVIGATION_MANAGER_CLOSING, NAVIGATION_MANAGER_EPOCH
    NAVIGATION_MANAGER_EPOCH += 1
    if NAVIGATION_MANAGER_CLOSING is not None:
        return NAVIGATION_MANAGER_CLOSING
    manager = NAVIGATION_MANAGER
    if manager is not None:
        NAVIGATION_MANAGER = None
        NAVIGATION_MANAGER_CLOSING = manager
    return manager


def _clear_navigation_manager_closing(manager) -> None:
    global NAVIGATION_MANAGER_CLOSING
    if NAVIGATION_MANAGER_CLOSING is manager:
        NAVIGATION_MANAGER_CLOSING = None


def _navigation_deadline_error(deadline: float):
    try:
        check_deadline(deadline)
    except TimeoutError as error:
        return error
    return None


def _reacquire_navigation_manager_lock(deadline: float, deadline_error) -> None:
    """Past the deadline the lock is taken only if it is free right now."""
    if deadline_error is None:
        _acquire_navigation_manager_lock(deadline)
        return
    if not NAVIGATION_MANAGER_LOCK.acquire(blocking=False):
        raise deadline_error


def close_navigation_session_manager(deadline: float) -> None:
    _acquire_navigation_manager_lock(deadline)
    try:
        manager = _detach_navigation_manager()
    finally:
        NAVIGATION_MANAGER_LOCK.release()
    if manager is None:
        return
    check_deadline(deadline)
    manager.close_all(deadline=deadline)
    deadline_error = _navigation_deadline_error(deadline)
    _reacquire_navigation_manager_lock(deadline, deadline_error)
    try:
        _clear_navigation_manager_closing(manager)
    finally:
        NAVIGATION_MANAGER_LOCK.release()
    if deadline_error is not None:
        raise deadline_error


def same_filesystem_path(
    left: Path,
    right: Path,
    *,
    deadline: float | None = None,
) -> bool:
    check_deadline(deadline)
    try:
        same = Path(left).samefile(Path(right))
    except OSError:
        same = False
    check_deadline(deadline)
    return same


def _navigation_capability(mode: str):
    from code_intelligence import Capability

    if mode in _PRECISE_MODE_CAPABILITY:
        return Capability[_PRECISE_MODE_CAPABILITY[mode]]
    if mode in {"callers", "callees"}:
        return Capability.CALLS
    raise ValueError("mode is not a precise navigation mode")


def normalized_navigation_failure(
    *,
    directory: str | None,
    mode: str,
    status: str,
    warning: str,
    offset: int,
    limit: int,
    scope=None,
    provider: str | None = None,
    provider_version: str | None = None,
    readiness: str = "not_ready",
) -> dict:
    capability = _navigation_capability(mode)
    return {
        "directory": directory,
        "mode": mode,
        "status": status,
        "freshness": {
            "workspace_revision_before": "",
            "workspace_revision_after": "",
            "current": "",
        },
        "provider": {"name": provider, "version": provider_version},
        "symbol": None,
        "total": 0,
        "requested_capability": capability.value,
        "effective_capability": None,
        "position_encoding": None,
        "readiness": readiness,
        "repository": {
            "repository_id": None if scope is None else scope.repository_id,
            "checkout_id": None if scope is None else scope.checkout_id,
        },
        "document_version": None,
        "offset": offset,
        "limit": limit,
        "truncated": False,
        "omitted": 0,
        "next_offset": None,
        "resolution": "unresolved",
        "groups": [],
        "diagnostics": [],
        "hover": None,
        "provenance": [],
        "warnings": (warning,),
    }


def _check_navigation_stop(deadline: float | None) -> None:
    check_deadline(deadline)
    if mcp_server_workers.operation_cancelled()():
        raise TimeoutError("navigation operation cancelled")
    check_deadline(deadline)


def _opened_navigation_generation(scope, deadline: float | None):
    """The generation, or None when there is none or it cannot be read.

    A generation that cannot be opened degrades exactly as a missing one does
    (audit 3, B26): all three callers of `open_navigation_graph` answer with
    structural or empty evidence, and none of their return types has anywhere
    to carry a reason.
    """
    from llm_wiki import code_graph_generation

    try:
        return code_graph_generation.active_evidence_graph(
            Path(scope.checkout_root),
            read_only=True,
            deadline=deadline,
            cancelled=mcp_server_workers.operation_cancelled(),
        )
    except code_graph_generation.GenerationUnreadable:
        return None


def open_navigation_graph(scope, deadline: float | None):
    _check_navigation_stop(deadline)
    graph = _opened_navigation_generation(scope, deadline)
    _check_navigation_stop(deadline)
    if graph is None:
        return None
    graph_scope = getattr(graph, "repository_scope", None)
    if (
        graph_scope is None
        or graph_scope.repository_id != scope.repository_id
        or graph_scope.checkout_id != scope.checkout_id
    ):
        graph.close()
        _check_navigation_stop(deadline)
        return None
    return graph


def _navigation_relative_path(
    scope,
    span: dict,
    *,
    deadline: float | None,
) -> str:
    from lsp_security import validate_repository_relative_path

    _check_navigation_stop(deadline)
    relative = span.get("relative_path")
    if not isinstance(relative, str):
        relative = _file_relative_path(scope, span, deadline=deadline)
    normalized = validate_repository_relative_path(relative)
    _check_navigation_stop(deadline)
    return normalized


def _file_relative_path(scope, span: dict, *, deadline: float | None) -> str:
    """A span that names only its `file`: relative as given, absolute under the checkout."""
    file_value = span.get("file")
    if not isinstance(file_value, str):
        raise ValueError("graph span has no source path")
    file_path = Path(file_value)
    if not file_path.is_absolute():
        return file_value
    return _checkout_relative(scope, file_path, deadline=deadline)


def _checkout_relative(scope, file_path: Path, *, deadline: float | None) -> str:
    root = Path(scope.checkout_root).resolve(strict=True)
    _check_navigation_stop(deadline)
    source = file_path.resolve(strict=True)
    _check_navigation_stop(deadline)
    return source.relative_to(root).as_posix()


def navigation_source_bytes(
    scope,
    relative_path: str,
    *,
    deadline: float | None,
) -> bytes:
    from lsp_security import read_repository_source_bytes

    _check_navigation_stop(deadline)
    content = read_repository_source_bytes(
        scope,
        relative_path,
        max_bytes=MAX_NAVIGATION_SOURCE_BYTES,
        deadline=deadline,
    )
    _check_navigation_stop(deadline)
    content.decode("utf-8", errors="strict")
    _check_navigation_stop(deadline)
    return content


class NavigationSourceCache:
    """The sources one answer read, least recently read evicted first.

    It used to refuse a source once the cache was full, and that source's spans
    left the answer without a count. Evicting instead keeps every span: a source
    read again costs one bounded read under the answer's deadline.
    """

    __slots__ = ("_bytes", "_values")

    def __init__(self) -> None:
        self._values: OrderedDict[tuple[str, str, str], tuple[bytes, str]] = OrderedDict()
        self._bytes = 0

    def read(self, scope, relative_path: str, *, deadline: float | None):
        key = (scope.repository_id, scope.checkout_id, relative_path)
        cached = self._values.get(key)
        if cached is not None:
            self._values.move_to_end(key)
            return cached
        content = navigation_source_bytes(scope, relative_path, deadline=deadline)
        return self._remember(key, content)

    def _remember(self, key, content: bytes):
        cached = (content, hashlib.sha256(content).hexdigest())
        self._make_room(len(content))
        self._values[key] = cached
        self._bytes += len(content)
        return cached

    def _make_room(self, incoming: int) -> None:
        """Evict the least recently read sources until `incoming` bytes and one entry fit."""
        while self._values and self._over_budget(incoming):
            _key, (evicted, _digest) = self._values.popitem(last=False)
            self._bytes -= len(evicted)

    def _over_budget(self, incoming: int) -> bool:
        too_many = len(self._values) >= mcp_server_limits.MAX_NAVIGATION_GRAPH_FACTS
        return too_many or self._bytes + incoming > mcp_server_limits.MAX_NAVIGATION_SOURCE_CACHE_BYTES


def _navigation_digest(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return None


def navigation_location_from_span(
    scope,
    span: dict,
    *,
    source_kind: str,
    require_span_hash: bool,
    metadata: dict | None,
    graph_version: str,
    deadline: float | None,
    source_cache: NavigationSourceCache | None = None,
):
    try:
        return _resolved_navigation_location(
            scope,
            span,
            source_kind=source_kind,
            require_span_hash=require_span_hash,
            metadata=metadata,
            graph_version=graph_version,
            deadline=deadline,
            source_cache=_navigation_cache(source_cache),
        )
    except TimeoutError:
        raise
    except (OSError, TypeError, UnicodeError, ValueError):
        return None


def _navigation_span_source(scope, span: dict, deadline, source_cache):
    """Return (relative path, bytes) while the recorded source digest holds."""
    relative_path = _navigation_relative_path(scope, span, deadline=deadline)
    expected_source_sha256 = _navigation_digest(span.get("source_sha256"))
    if expected_source_sha256 is None:
        return None
    cached = source_cache.read(scope, relative_path, deadline=deadline)
    if cached is None or cached[1] != expected_source_sha256:
        return None
    return relative_path, cached[0]


def _span_range_invalid(byte_start: int, byte_end: int, size: int) -> bool:
    if byte_start < 0 or byte_end <= byte_start:
        return True
    return byte_end > size


def _span_bounds(span: dict, content: bytes):
    byte_start = span.get("byte_start")
    byte_end = span.get("byte_end")
    if not _is_integer_value(byte_start) or not _is_integer_value(byte_end):
        return None
    if _span_range_invalid(byte_start, byte_end, len(content)):
        return None
    return byte_start, byte_end


def _span_kind_valid(source_kind: str, require_span_hash: bool) -> bool:
    if source_kind not in {"evidence", "occurrence"}:
        return False
    return require_span_hash == (source_kind == "evidence")


def _span_hash_matches(span: dict, content: bytes, bounds) -> bool:
    expected = span.get("span_sha256")
    if _navigation_digest(expected) is None:
        return False
    return hashlib.sha256(content[bounds[0]:bounds[1]]).hexdigest() == expected


def _validated_span_bounds(
    span: dict, content: bytes, source_kind: str, require_span_hash: bool
):
    if not _span_kind_valid(source_kind, require_span_hash):
        return None
    bounds = _span_bounds(span, content)
    if bounds is None or (
        require_span_hash and not _span_hash_matches(span, content, bounds)
    ):
        return None
    return bounds


def _span_line_position(document, content: bytes, byte_start: int, deadline):
    """Return the 1-based line and byte column that hold byte_start."""
    for line_number, (line_start, line_end) in enumerate(document.line_spans, 1):
        _check_navigation_stop(deadline)
        if line_start <= byte_start <= line_end:
            content[line_start:byte_start].decode("utf-8", errors="strict")
            return line_number, byte_start - line_start
    return None


def _reported_line_disagrees(span: dict, line: int) -> bool:
    reported_line = span.get("line_start")
    if reported_line is None:
        return False
    if not _is_integer_value(reported_line):
        return True
    return reported_line != line


def _span_position(relative_path: str, content: bytes, byte_start: int, span, deadline):
    from lsp_positions import SourceDocument

    _check_navigation_stop(deadline)
    document = SourceDocument.from_bytes(relative_path, content)
    _check_navigation_stop(deadline)
    position = _span_line_position(document, content, byte_start, deadline)
    if position is None:
        return None
    if _reported_line_disagrees(span, position[0]):
        return None
    return position


def _containing_symbol(metadata) -> str | None:
    if not isinstance(metadata, dict):
        return None
    owner = metadata.get("owner")
    if isinstance(owner, str) and owner:
        return owner
    return None


def _span_signature(span: dict, content: bytes, bounds) -> str | None:
    if span.get("role") not in {"definition", "declaration"}:
        return None
    return content[bounds[0]:bounds[1]].decode("utf-8", errors="strict")


def _navigation_location(
    relative_path, content, bounds, position, span, metadata, graph_version
):
    from code_intelligence import PositionRange
    from code_navigation import NavigationLocation, Provenance, ResolutionLabel

    line, character = position
    return NavigationLocation(
        relative_path,
        PositionRange(bounds[0], bounds[1]),
        line,
        character,
        _containing_symbol(metadata),
        _span_signature(span, content, bounds),
        ResolutionLabel.GRAPH_CANDIDATE,
        (Provenance("graph", "evidence-graph", graph_version, "graph_candidate"),),
    )


def _resolved_navigation_location(
    scope,
    span: dict,
    *,
    source_kind: str,
    require_span_hash: bool,
    metadata: dict | None,
    graph_version: str,
    deadline: float | None,
    source_cache,
):
    source = _navigation_span_source(scope, span, deadline, source_cache)
    if source is None:
        return None
    relative_path, content = source
    bounds = _validated_span_bounds(span, content, source_kind, require_span_hash)
    if bounds is None:
        return None
    return _located_navigation_span(
        relative_path, content, bounds, span, metadata, graph_version, deadline
    )


def _located_navigation_span(
    relative_path, content, bounds, span, metadata, graph_version, deadline
):
    """The location for a validated span, or None when its position is unreadable."""
    position = _span_position(relative_path, content, bounds[0], span, deadline)
    if position is None:
        return None
    _check_navigation_stop(deadline)
    return _navigation_location(
        relative_path, content, bounds, position, span, metadata, graph_version
    )


def _bounded_navigation_locations(locations, deadline: float | None):
    unique = {}
    for location in locations:
        _check_navigation_stop(deadline)
        if location is None:
            continue
        key = (
            location.path,
            location.range.byte_start,
            location.range.byte_end,
            location.line,
            location.character,
        )
        unique.setdefault(key, location)
        if len(unique) >= mcp_server_limits.MAX_NAVIGATION_GRAPH_FACTS:
            break
    _check_navigation_stop(deadline)
    return tuple(unique[key] for key in sorted(unique))


def _graph_nodes_for_symbol(graph, symbol: str, deadline: float | None):
    _check_navigation_stop(deadline)
    nodes = graph.find_nodes(
        kinds=("class", "function", "method"),
        name=symbol,
        max_rows=mcp_server_limits.MAX_NAVIGATION_GRAPH_FACTS,
        deadline=deadline,
    )
    _check_navigation_stop(deadline)
    return tuple(nodes[:mcp_server_limits.MAX_NAVIGATION_GRAPH_FACTS])


def _navigation_cache(source_cache: NavigationSourceCache | None):
    if source_cache is None:
        return NavigationSourceCache()
    return source_cache


def _graph_generation_version(graph) -> str:
    return str(getattr(graph, "generation_id", None) or "structural")


def _graph_node_metadata(node):
    metadata = node.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    return None


def _declaration_locations_for_node(
    graph, node, scope, version, deadline, source_cache, remaining
):
    """Return this node's declaration locations and how much budget they spent."""
    occurrences = graph.occurrences(
        node["node_id"],
        max_rows=remaining,
        deadline=deadline,
    )
    _check_navigation_stop(deadline)
    metadata = _graph_node_metadata(node)
    bounded = occurrences[:remaining]
    locations = []
    for occurrence in bounded:
        _check_navigation_stop(deadline)
        if occurrence.get("role") not in {"definition", "declaration"}:
            continue
        locations.append(
            navigation_location_from_span(
                scope,
                occurrence,
                source_kind="occurrence",
                require_span_hash=False,
                metadata=metadata,
                graph_version=version,
                deadline=deadline,
                source_cache=source_cache,
            )
        )
    return locations, len(bounded)


def _collected_declaration_locations(graph, symbol, scope, deadline, source_cache):
    version = _graph_generation_version(graph)
    locations = []
    remaining = mcp_server_limits.MAX_NAVIGATION_GRAPH_FACTS
    for node in _graph_nodes_for_symbol(graph, symbol, deadline):
        _check_navigation_stop(deadline)
        if remaining <= 0:
            break
        found, spent = _declaration_locations_for_node(
            graph, node, scope, version, deadline, source_cache, remaining
        )
        locations.extend(found)
        remaining -= spent
    return _bounded_navigation_locations(locations, deadline)


def graph_declaration_locations(
    symbol: str,
    scope,
    deadline: float | None,
    source_cache: NavigationSourceCache | None = None,
):
    graph = open_navigation_graph(scope, deadline)
    if graph is None:
        return ()
    try:
        return _collected_declaration_locations(
            graph, symbol, scope, deadline, _navigation_cache(source_cache)
        )
    finally:
        graph.close()
        _check_navigation_stop(deadline)


def _call_edge_source_key(direction: str) -> str:
    if direction == "incoming":
        return "target_node_id"
    return "source_node_id"


def _anchored_call_edges(graph, node_key: str, node_ids: set, deadline) -> list:
    """The CALLS edges whose `node_key` end is one of these nodes, read anchored in SQL.

    Reading every CALLS edge up to the fact bound and filtering afterwards lost a
    symbol's calls silently once the repository held more CALLS edges than the bound
    (law 9 class d, docs/research/2026-09-27-a-cut-says-what-it-left-out.md). The ids go
    in slices of `evidence_graph.MAX_NODE_FILTER`, which the reader refuses past.
    """
    from evidence_graph import MAX_NODE_FILTER

    ordered = sorted(node_ids)
    edges: list = []
    for start in range(0, len(ordered), MAX_NODE_FILTER):
        _check_navigation_stop(deadline)
        anchor = {f"{node_key}s": ordered[start : start + MAX_NODE_FILTER]}
        edges.extend(
            graph.edges(edge_types=("CALLS",), max_rows=mcp_server_limits.MAX_NAVIGATION_GRAPH_FACTS, deadline=deadline, **anchor)
        )
    return edges


def _matching_call_edges(edges, source_key: str, node_ids: set, deadline):
    for edge in edges:
        _check_navigation_stop(deadline)
        if edge.get(source_key) in node_ids:
            yield edge


def _call_evidence_locations(
    graph, edge, scope, version, deadline, source_cache, remaining
):
    """Return this edge's evidence locations and how much budget they spent."""
    evidence = graph.evidence_spans(
        assertion_id=edge["assertion_id"],
        max_rows=remaining,
        deadline=deadline,
    )
    _check_navigation_stop(deadline)
    bounded = evidence[:remaining]
    locations = []
    for span in bounded:
        _check_navigation_stop(deadline)
        locations.append(
            navigation_location_from_span(
                scope,
                span,
                source_kind="evidence",
                require_span_hash=True,
                metadata=None,
                graph_version=version,
                deadline=deadline,
                source_cache=source_cache,
            )
        )
    return locations, len(bounded)


def collected_call_locations(graph, symbol, scope, direction, deadline, source_cache):
    node_ids = {
        node["node_id"] for node in _graph_nodes_for_symbol(graph, symbol, deadline)
    }
    _check_navigation_stop(deadline)
    source_key = _call_edge_source_key(direction)
    edges = _anchored_call_edges(graph, source_key, node_ids, deadline)
    _check_navigation_stop(deadline)
    version = _graph_generation_version(graph)
    locations = []
    remaining = mcp_server_limits.MAX_NAVIGATION_GRAPH_FACTS
    for edge in _matching_call_edges(edges, source_key, node_ids, deadline):
        if remaining <= 0:
            break
        found, spent = _call_evidence_locations(
            graph, edge, scope, version, deadline, source_cache, remaining
        )
        locations.extend(found)
        remaining -= spent
    return _bounded_navigation_locations(locations, deadline)


def graph_call_locations(
    symbol: str,
    scope,
    *,
    direction: str,
    deadline: float | None,
    source_cache: NavigationSourceCache | None = None,
):
    graph = open_navigation_graph(scope, deadline)
    if graph is None:
        return ()
    try:
        return collected_call_locations(
            graph,
            symbol,
            scope,
            direction,
            deadline,
            _navigation_cache(source_cache),
        )
    finally:
        graph.close()
        _check_navigation_stop(deadline)


def _navigation_anchor_symbol(
    scope,
    path: str,
    line: int,
    character: int,
    *,
    byte_offset: int | None = None,
    deadline: float | None,
    source_cache: NavigationSourceCache | None = None,
) -> str | None:
    try:
        return _resolved_anchor_symbol(
            scope,
            path,
            line,
            character,
            byte_offset,
            deadline,
            _navigation_cache(source_cache),
        )
    except TimeoutError:
        raise
    except (OSError, TypeError, UnicodeError, ValueError):
        return None


def _anchor_identifier(line_bytes: bytes, character: int, deadline) -> str | None:
    for match in re.finditer(rb"[A-Za-z_][A-Za-z0-9_]*", line_bytes):
        _check_navigation_stop(deadline)
        if match.start() <= character <= match.end():
            return match.group().decode("ascii")
    return None


def _resolved_anchor_symbol(
    scope, path, line, character, byte_offset, deadline, source_cache
) -> str | None:
    from lsp_positions import SourceDocument

    cached = source_cache.read(scope, path, deadline=deadline)
    if cached is None:
        return None
    content, _source_sha256 = cached
    _check_navigation_stop(deadline)
    document = SourceDocument.from_bytes(path, content)
    _check_navigation_stop(deadline)
    anchor = document.validate_anchor(line=line, character=character)
    if byte_offset is not None and anchor.byte_offset != byte_offset:
        return None
    line_start, line_end = document.line_spans[line - 1]
    return _anchor_identifier(content[line_start:line_end], character, deadline)


def navigation_structural_candidates(request, deadline: float):
    from code_intelligence import Capability

    _check_navigation_stop(deadline)
    source_cache = NavigationSourceCache()
    symbol = _navigation_anchor_symbol(
        request.repository,
        request.path,
        request.line,
        request.character,
        deadline=deadline,
        source_cache=source_cache,
    )
    if symbol is None:
        return ()
    return _structural_candidates_for(
        Capability, request, symbol, deadline, source_cache
    )


def _structural_call_candidates(request, symbol, deadline, source_cache):
    if request.direction not in {"incoming", "outgoing"}:
        return ()
    return graph_call_locations(
        symbol,
        request.repository,
        direction=request.direction,
        deadline=deadline,
        source_cache=source_cache,
    )


def _structural_candidates_for(Capability, request, symbol, deadline, source_cache):
    handlers = {
        Capability.DEFINITIONS: lambda: graph_declaration_locations(
            symbol,
            request.repository,
            deadline,
            source_cache,
        ),
        Capability.REFERENCES: lambda: graph_call_locations(
            symbol,
            request.repository,
            direction="incoming",
            deadline=deadline,
            source_cache=source_cache,
        ),
        Capability.CALLS: lambda: _structural_call_candidates(
            request, symbol, deadline, source_cache
        ),
    }
    handler = handlers.get(request.capability)
    if handler is None:
        return ()
    return handler()


def navigation_symbol_resolver(symbol, scope, deadline):
    if not isinstance(symbol, str) or not symbol:
        return ()
    return graph_declaration_locations(
        symbol,
        scope,
        deadline,
        NavigationSourceCache(),
    )


def graph_node_ids_at_anchor(
    graph,
    symbol,
    anchor,
    scope,
    deadline,
    source_cache: NavigationSourceCache | None = None,
):
    source_cache = _navigation_cache(source_cache)
    version = _graph_generation_version(graph)
    identifiers = set()
    remaining = mcp_server_limits.MAX_NAVIGATION_GRAPH_FACTS
    for node in _graph_nodes_for_symbol(graph, symbol, deadline):
        _check_navigation_stop(deadline)
        if remaining <= 0:
            break
        covers, spent = _node_covers_anchor(
            graph, node, anchor, scope, version, deadline, source_cache, remaining
        )
        remaining -= spent
        if covers:
            identifiers.add(node["node_id"])
    return identifiers


def _location_covers_anchor(location, anchor) -> bool:
    if location is None or location.path != anchor.path:
        return False
    return location.range.byte_start <= anchor.byte_offset < location.range.byte_end


def _node_covers_anchor(
    graph, node, anchor, scope, version, deadline, source_cache, remaining
):
    """Report whether this node declares the anchor, and what budget it spent."""
    occurrences = graph.occurrences(
        node["node_id"],
        max_rows=remaining,
        deadline=deadline,
    )
    _check_navigation_stop(deadline)
    bounded = occurrences[:remaining]
    covers = False
    for occurrence in bounded:
        _check_navigation_stop(deadline)
        if occurrence.get("role") not in {"definition", "declaration"}:
            continue
        location = navigation_location_from_span(
            scope,
            occurrence,
            source_kind="occurrence",
            require_span_hash=False,
            metadata=None,
            graph_version=version,
            deadline=deadline,
            source_cache=source_cache,
        )
        covers = covers or _location_covers_anchor(location, anchor)
    return covers, len(bounded)


def navigation_edge_verifier(source, target, scope, deadline):
    source_cache = NavigationSourceCache()
    source_symbol = _navigation_anchor_symbol(
        scope,
        source.path,
        source.line,
        source.utf8_character,
        byte_offset=source.byte_offset,
        deadline=deadline,
        source_cache=source_cache,
    )
    target_symbol = _navigation_anchor_symbol(
        scope,
        target.path,
        target.line,
        target.utf8_character,
        byte_offset=target.byte_offset,
        deadline=deadline,
        source_cache=source_cache,
    )
    if source_symbol is None or target_symbol is None:
        return False
    graph = open_navigation_graph(scope, deadline)
    if graph is None:
        return False
    try:
        return _verified_call_edge(
            graph,
            (source, source_symbol),
            (target, target_symbol),
            scope,
            deadline,
            source_cache,
        )
    finally:
        graph.close()
        _check_navigation_stop(deadline)


def _edge_connects(edges, source_ids: set, target_ids: set, deadline) -> bool:
    for edge in edges:
        _check_navigation_stop(deadline)
        if (
            edge.get("source_node_id") in source_ids
            and edge.get("target_node_id") in target_ids
        ):
            return True
    return False


def _verified_call_edge(graph, source, target, scope, deadline, source_cache) -> bool:
    source_anchor, source_symbol = source
    target_anchor, target_symbol = target
    source_ids = graph_node_ids_at_anchor(
        graph, source_symbol, source_anchor, scope, deadline, source_cache
    )
    target_ids = graph_node_ids_at_anchor(
        graph, target_symbol, target_anchor, scope, deadline, source_cache
    )
    _check_navigation_stop(deadline)
    edges = _anchored_call_edges(graph, "source_node_id", source_ids, deadline)
    _check_navigation_stop(deadline)
    return _edge_connects(edges, source_ids, target_ids, deadline)


def get_precise_architecture(
    directory: str,
    *,
    mode: str,
    path: str,
    line: int,
    character: int,
    offset: int = 0,
    limit: int = 10,
    deadline: float | None = None,
) -> dict:
    """Route precise modes through the owned CodeNavigation facade."""
    effective_deadline = _navigation_effective_deadline(deadline)
    progress = {
        "stage": "directory",
        "scope": None,
        "resolved": None,
        "provider": None,
    }
    try:
        return _precise_architecture_result(
            progress,
            directory,
            mode,
            {"path": path, "line": line, "character": character},
            offset,
            limit,
            effective_deadline,
        )
    except TimeoutError:
        return normalized_navigation_failure(
            directory=_progress_directory(progress),
            mode=mode,
            status="timeout",
            warning="navigation_timeout",
            offset=offset,
            limit=limit,
            scope=progress["scope"],
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _navigation_stage_failure(progress, mode, offset, limit)


def _navigation_effective_deadline(deadline: float | None) -> float:
    if deadline is not None:
        return deadline
    return time.monotonic() + mcp_server_limits.MCP_LSP_STARTUP_SECONDS


def _progress_directory(progress: dict) -> str | None:
    resolved = progress["resolved"]
    if resolved is None:
        return None
    return str(resolved)


_NAVIGATION_STAGE_WARNINGS = {
    "manager": "navigation_provider_not_ready",
    "renderer": "navigation_render_failed",
    "source": "navigation_source_unavailable",
}

_NAVIGATION_PROVIDER_STAGES = {"manager", "facade", "renderer"}


def _navigation_stage_status(stage: str) -> str:
    if stage == "manager":
        return "not_ready"
    return "error"


def _navigation_stage_provider(stage: str, progress: dict) -> str | None:
    if stage in _NAVIGATION_PROVIDER_STAGES:
        return progress.get("provider")
    return None


def _navigation_stage_failure(progress: dict, mode, offset, limit) -> dict:
    stage = progress["stage"]
    return normalized_navigation_failure(
        directory=_progress_directory(progress),
        mode=mode,
        status=_navigation_stage_status(stage),
        warning=_NAVIGATION_STAGE_WARNINGS.get(stage, "navigation_setup_failed"),
        offset=offset,
        limit=limit,
        scope=progress["scope"],
        provider=_navigation_stage_provider(stage, progress),
    )


def _navigation_manager_epoch(deadline: float, cancelled) -> int:
    _check_navigation_manager_stop(deadline, cancelled)
    _acquire_navigation_manager_lock(deadline)
    try:
        _check_navigation_manager_stop(deadline, cancelled)
        epoch = NAVIGATION_MANAGER_EPOCH
    finally:
        NAVIGATION_MANAGER_LOCK.release()
    _check_navigation_manager_stop(deadline, cancelled)
    return epoch


def _navigation_scope(resolved: Path, deadline: float, cancelled):
    from repository_scope import resolve_repository_scope

    check_deadline(deadline)
    scope = resolve_repository_scope(resolved, deadline=deadline, cancelled=cancelled)
    check_deadline(deadline)
    return scope


def _validated_navigation_source(scope, path: str, deadline: float) -> str:
    from lsp_security import (
        resolve_repository_source,
        validate_repository_relative_path,
    )

    check_deadline(deadline)
    normalized_path = validate_repository_relative_path(path)
    check_deadline(deadline)
    resolve_repository_source(scope, normalized_path, must_exist=True)
    check_deadline(deadline)
    return normalized_path


def navigation_profile(normalized_path: str):
    """The managed language server that owns this file.

    Routing is by file suffix through the profile registry -- the shape every
    multi-language LSP client uses, and the registry refuses a suffix claimed by
    two profiles, so the table is a function by construction. See
    `docs/research/2026-08-28-wiring-a-second-language-server.md`, question 1.

    A suffix no profile claims is still handed a Pyright session, because
    `CodeNavigation` requires one; `CodeNavigation.query` answers such a file
    `unsupported` before the session is asked anything (audit 2026-09-26 C-9).
    """
    from lsp_profiles import PYRIGHT_PROFILE, profile_for_path

    return profile_for_path(Path(normalized_path)) or PYRIGHT_PROFILE


def _navigation_session(scope, deadline: float, manager_epoch: int, cancelled, profile):
    manager = navigation_session_manager(deadline, manager_epoch, cancelled)
    check_deadline(deadline)
    session = manager.get(scope, deadline=deadline, profile=profile)
    check_deadline(deadline)
    return session


_NAVIGATION_MODE_DIRECTIONS = {"callers": "incoming", "callees": "outgoing"}


def _navigation_query_result(
    scope, session, mode, normalized_path, anchor, offset, limit, deadline
):
    from code_navigation import CodeNavigation, NavigationRequest

    navigation = CodeNavigation(
        scope,
        session,
        session.identity,
        structural_candidates=navigation_structural_candidates,
        symbol_resolver=navigation_symbol_resolver,
        edge_verifier=navigation_edge_verifier,
    )
    check_deadline(deadline)
    request = NavigationRequest(
        scope,
        _navigation_capability(mode),
        normalized_path,
        anchor["line"],
        anchor["character"],
        offset=offset,
        limit=limit,
        direction=_NAVIGATION_MODE_DIRECTIONS.get(mode),
    )
    check_deadline(deadline)
    result = navigation.query(request, deadline=deadline)
    check_deadline(deadline)
    return result


def _navigation_provider_view(rendered: dict, session) -> dict:
    """Name the server that actually answered, not the one the facade is typed for.

    `CodeNavigation.provider` returns the literal `"pyright"`. Correcting it here
    rather than there is a named residue: the complexity gate refuses
    `scripts/code_navigation.py` wholesale over 39 pre-existing findings, and
    the envelope the agent reads is built here anyway.
    """
    provider = dict(rendered.get("provider") or {})
    if provider.get("name") is None:
        return provider
    provider["name"] = session.profile.name
    return provider


def _navigation_limit_warnings(rendered: dict, session) -> tuple[str, ...]:
    """Carry the session's capability limits into the answer, by name.

    The contract asks the precise tier to report readiness and capability limits
    and fall back. Readiness was always in the envelope; the limits were not, so
    an uninstalled server degraded without saying why. A qualified session
    reports none, so an installed Pyright answer is unchanged.
    """
    warnings = tuple(rendered.get("warnings") or ())
    limits = tuple(session.degradation_codes)
    return (*warnings, *(limit for limit in limits if limit not in warnings))


def _navigation_provenance_named(data: dict, session) -> dict:
    """Rename in place, and leave the key absent when the answer carried none.

    A structural or refused answer has no provenance at all; adding an empty one
    would change the envelope's shape for every Python answer that never had it.
    """
    rows = data.get("provenance")
    if not rows:
        return data
    data["provenance"] = _navigation_provenance_view(data, session)
    return data


def _navigation_provenance_view(rendered: dict, session) -> tuple:
    """Name the server in the provenance too, not only in the provider block.

    The same literal `"pyright"` reaches the envelope twice, and correcting one
    of them left a TypeScript answer carrying `provenance[].provider: "pyright"`
    -- a false statement about where the answer came from, in the field whose
    whole job is to say. Corrected at the same seam and for the same named
    reason as `_navigation_provider_view`: `scripts/code_navigation.py` is
    refused wholesale by the complexity gate over 39 pre-existing findings.

    Only `source: "lsp"` rows are touched. Structural rows are produced by this
    build's own graph and already name themselves correctly.
    """
    rows = tuple(rendered.get("provenance") or ())
    return tuple(_provenance_row_named(row, session) for row in rows)


def _provenance_row_named(row: object, session) -> object:
    if not isinstance(row, dict) or row.get("source") != "lsp":
        return row
    named = dict(row)
    named["provider"] = session.profile.name
    return named


def navigation_named_by_session(data: dict, session) -> dict:
    if session is None:
        return data
    data["provider"] = _navigation_provider_view(data, session)
    data["warnings"] = _navigation_limit_warnings(data, session)
    return _navigation_provenance_named(data, session)


def _rendered_navigation_result(
    resolved: Path, mode, result, deadline: float, session=None
) -> dict:
    from code_navigation_renderer import render_navigation

    rendered = render_navigation(result)
    check_deadline(deadline)
    data = {"directory": str(resolved), "mode": mode, **rendered}
    check_deadline(deadline)
    data = navigation_named_by_session(data, session)
    data = mcp_server_sanitize.sanitize_navigation_data(data)
    check_deadline(deadline)
    return data


def _precise_architecture_result(
    progress: dict, directory, mode, anchor: dict, offset, limit, deadline: float
) -> dict:
    cancelled = mcp_server_workers.operation_cancelled()
    manager_epoch = _navigation_manager_epoch(deadline, cancelled)
    resolved, error = mcp_server_validation.validated_code_directory(directory, deadline=deadline)
    progress["resolved"] = resolved
    check_deadline(deadline)
    if error or resolved is None:
        return normalized_navigation_failure(
            directory=None,
            mode=mode,
            status="error",
            warning="navigation_directory_invalid",
            offset=offset,
            limit=limit,
        )
    progress["stage"] = "scope"
    scope = _navigation_scope(resolved, deadline, cancelled)
    progress["scope"] = scope
    if not same_filesystem_path(
        resolved, Path(scope.checkout_root), deadline=deadline
    ):
        return normalized_navigation_failure(
            directory=str(resolved),
            mode=mode,
            status="error",
            warning="navigation_directory_not_checkout_root",
            offset=offset,
            limit=limit,
            scope=scope,
        )
    progress["stage"] = "source"
    normalized_path = _validated_navigation_source(scope, anchor["path"], deadline)
    profile = navigation_profile(normalized_path)
    progress["provider"] = profile.name
    progress["stage"] = "manager"
    session = _navigation_session(scope, deadline, manager_epoch, cancelled, profile)
    progress["stage"] = "facade"
    result = _navigation_query_result(
        scope, session, mode, normalized_path, anchor, offset, limit, deadline
    )
    progress["stage"] = "renderer"
    return _rendered_navigation_result(resolved, mode, result, deadline, session)
