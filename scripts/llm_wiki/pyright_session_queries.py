"""Location, symbol, hover and workspace-symbol queries against one open document."""

import dataclasses
import time
from collections.abc import Mapping
from dataclasses import dataclass

from code_intelligence import PositionEncoding
from llm_wiki.pyright_session_ranges import (
    MAX_CALL_ITEM_TEXT_BYTES,
    BoundedLocations,
    bounded_text,
    location_fields,
    location_key,
    location_payload,
    lsp_coordinate,
    lsp_range,
    range_contains,
    symbol_tags_ok,
)
from llm_wiki.pyright_session_values import (
    LspLocation,
    OpenDocument,
    ProviderHover,
    ProviderLocations,
    validated_deadline,
)
from lsp_positions import LspPosition, LspRange, SourceAnchor, SourceDocument
from lsp_process import LspProcess
from lsp_protocol import MAX_HOVER_BYTES, MAX_LOCATIONS
from lsp_security import normalize_provider_uri


def _bounded_hover_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        return None
    if size > MAX_HOVER_BYTES:
        return None
    return value


def _hover_kind_labelled(value: Mapping[str, object], text: str) -> str | None:
    if value.get("kind") in {"plaintext", "markdown"}:
        return text
    return None


def _hover_fragment_labelled(value: Mapping[str, object], text: str) -> str | None:
    """A fragment is usable when its kind or its language names something real."""
    if "kind" in value:
        return _hover_kind_labelled(value, text)
    language = value.get("language")
    if not isinstance(language, str) or not language:
        return None
    return text


def _hover_fragment_mapping(value: Mapping[str, object]) -> str | None:
    text = _bounded_hover_string(value.get("value"))
    if text is None:
        return None
    return _hover_fragment_labelled(value, text)


def _hover_fragment(value: object) -> str | None:
    if isinstance(value, str):
        return _bounded_hover_string(value)
    if not isinstance(value, dict):
        return None
    return _hover_fragment_mapping(value)


def _hover_fragments(items: list[object]) -> tuple[list[str], bool]:
    """Every readable fragment, and whether any had to be dropped."""
    fragments: list[str] = []
    partial = False
    for item in items:
        fragment = _hover_fragment(item)
        if fragment is None:
            partial = True
            continue
        fragments.append(fragment)
    return fragments, partial


def _bounded_join(fragments: list[str], partial: bool) -> tuple[str | None, bool]:
    joined = "\n\n".join(fragments)
    if _bounded_hover_string(joined) is None:
        return None, True
    return joined, partial


def _joined_hover_contents(value: list[object]) -> tuple[str | None, bool]:
    """The list form of hover contents, joined and bounded."""
    if len(value) > 1024:
        return None, True
    fragments, partial = _hover_fragments(value)
    if not fragments:
        return None, True
    return _bounded_join(fragments, partial)


def _hover_contents(value: object) -> tuple[str | None, bool]:
    if isinstance(value, list):
        return _joined_hover_contents(value)
    fragment = _hover_fragment(value)
    return fragment, fragment is None


@dataclass
class _SymbolWalk:
    """A bounded depth-first pass over a document-symbol tree."""

    stack: list[object]
    partial: bool
    visited: int = 0
    seen_nodes: set[int] = dataclasses.field(default_factory=set)
    seen_keys: set[tuple[object, ...]] = dataclasses.field(default_factory=set)
    locations: list[LspLocation] = dataclasses.field(default_factory=list)

    def drop(self) -> None:
        """Record that something in the tree could not be used."""
        self.partial = True

    def add(self, location: LspLocation) -> None:
        key = location_key(location)
        if key in self.seen_keys:
            return
        self.seen_keys.add(key)
        self.locations.append(location)

    def first_visit(self, value: object) -> bool:
        """True the first time this exact node is seen."""
        node_id = id(value)
        if node_id in self.seen_nodes:
            return False
        self.seen_nodes.add(node_id)
        return True


def _push_symbol_children(walk: _SymbolWalk, value: Mapping[str, object]) -> None:
    """Queue the node's children, dropping whatever exceeds the walk's bound."""
    children = value.get("children")
    if children is None:
        return
    if not isinstance(children, list):
        walk.drop()
        return
    _push_bounded_children(walk, children)


def _push_bounded_children(walk: _SymbolWalk, children: list[object]) -> None:
    remaining = max(0, MAX_LOCATIONS - walk.visited - len(walk.stack))
    if len(children) > remaining:
        walk.drop()
    walk.stack.extend(reversed(children[:remaining]))


def _bounded_optional_text(value: object) -> bool:
    """An absent field is fine; a present one has to fit the text bound."""
    if value is None:
        return True
    return bounded_text(value, MAX_CALL_ITEM_TEXT_BYTES) is not None


def _symbol_flags_ok(value: Mapping[str, object]) -> bool:
    """The deprecated flag and the tag list, when present, are well formed."""
    deprecated = value.get("deprecated")
    if deprecated is not None and not isinstance(deprecated, bool):
        return False
    tags = value.get("tags")
    if tags is None:
        return True
    return symbol_tags_ok(tags)


def _symbol_fields_ok(value: Mapping[str, object]) -> bool:
    """Every scalar field of the symbol is present and within bounds."""
    name = bounded_text(value.get("name"), MAX_CALL_ITEM_TEXT_BYTES)
    kind = lsp_coordinate(value.get("kind"))
    if name is None or kind in {None, 0}:
        return False
    return (
        _bounded_optional_text(value.get("detail"))
        and _bounded_optional_text(value.get("containerName"))
        and _symbol_flags_ok(value)
    )


def _contained_selection(
    range_: LspRange | None, selection: LspRange | None, uri: str
) -> LspLocation | None:
    """The selection range, but only when the symbol's own range contains it."""
    if range_ is None or selection is None:
        return None
    if not range_contains(range_, selection):
        return None
    return LspLocation(uri, selection)


@dataclass(frozen=True)
class DocumentQuery:
    """The process, generation, document and encoding one query is bound to."""

    process: LspProcess
    generation: str
    document: OpenDocument
    epoch: int
    encoding: PositionEncoding


@dataclass(frozen=True)
class _WorkspaceState:
    """The session state a workspace-wide query was admitted against."""

    process: LspProcess | None
    generation: str | None
    readiness: str
    supported: bool
    initialized: bool
    current: bool


@dataclass(frozen=True)
class _WorkspaceQuery:
    """What one workspace-wide query is bound to."""

    process: LspProcess
    generation: str
    readiness: str
    epoch: int


def _workspace_query_ready(state: _WorkspaceState) -> bool:
    """The session holds a current, initialized generation to query."""
    if state.process is None or state.generation is None:
        return False
    return state.initialized and state.current


def _workspace_query_status(state: _WorkspaceState) -> str | None:
    """The status refusing a workspace query, or None when it may proceed."""
    if not _workspace_query_ready(state):
        return "not_ready"
    if not state.supported:
        return "unsupported"
    return None if state.readiness == "query_ready" else "not_ready"


def _check_symbol_query(query: str) -> None:
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    if len(query.encode("utf-8", errors="strict")) > 4096:
        raise ValueError("query exceeds 4096 bytes")


def _symbol_locations_payload(result: list[object]) -> tuple[list[object], bool]:
    """Every well-formed symbol's location, and whether any had to be dropped."""
    values: list[object] = []
    partial = len(result) > MAX_LOCATIONS
    for symbol in result[:MAX_LOCATIONS]:
        if not isinstance(symbol, dict) or "location" not in symbol:
            partial = True
            continue
        values.append(symbol["location"])
    return values, partial


def _hover_range(result: Mapping[str, object]) -> tuple[LspRange | None, bool]:
    """The hover's range, and whether it was readable."""
    if "range" not in result:
        return None, True
    range_ = lsp_range(result["range"])
    return range_, range_ is not None


def _hover_response(result: object) -> ProviderHover:
    """The hover the server reported, or an explicitly partial answer."""
    if result is None:
        return ProviderHover(None, None, False)
    if not isinstance(result, dict) or "contents" not in result:
        return ProviderHover(None, None, True)
    contents, partial = _hover_contents(result["contents"])
    range_, range_ok = _hover_range(result)
    return ProviderHover(contents, range_, partial or not range_ok)


def position_params(
    query: DocumentQuery, position: LspPosition
) -> dict[str, object]:
    """The textDocument/position pair every anchored request sends."""
    return {
        "textDocument": {"uri": query.document.source.uri},
        "position": {
            "line": position.line,
            "character": position.character,
        },
    }


class SessionQueries:
    """Answer location, symbol, hover and workspace-symbol queries."""

    def _located_at(self, uri: object, range_value: object) -> LspLocation | None:
        """One location from a URI this repository owns and a usable range."""
        if not isinstance(uri, str):
            return None
        range_ = lsp_range(range_value)
        if range_ is None:
            return None
        return self._owned_location(uri, range_)

    def _owned_location(self, uri: str, range_: LspRange) -> LspLocation | None:
        source = normalize_provider_uri(self._repository, uri)
        if source is None:
            return None
        return LspLocation(source.uri, range_)

    def _normalize_location(self, value: object) -> LspLocation | None:
        if not isinstance(value, dict):
            return None
        located = location_fields(value)
        if located is None:
            return None
        return self._located_at(located[0], located[1])

    def _normalize_locations(
        self,
        result: object,
    ) -> tuple[tuple[LspLocation, ...], bool]:
        raw = location_payload(result)
        if raw is None:
            return (), result is not None
        partial = len(raw) > MAX_LOCATIONS
        collected = BoundedLocations()
        for value in raw[:MAX_LOCATIONS]:
            location = self._normalize_location(value)
            if location is None:
                partial = True
                continue
            collected.add(location)
        return tuple(collected.locations), partial

    def _location_params(
        self, query: DocumentQuery, anchor: SourceAnchor, *, references: bool
    ) -> dict[str, object]:
        params = position_params(query, self._anchor_position(query, anchor))
        if references:
            params["context"] = {"includeDeclaration": True}
        return params

    def _location_feature_within_operation(
        self,
        anchor: SourceAnchor,
        *,
        capability: str,
        method: str,
        deadline: float,
        references: bool,
    ) -> ProviderLocations:
        query, status = self._begin_document_query(
            capability, anchor.path, deadline=deadline
        )
        if query is None:
            return ProviderLocations((), status, True)
        params = self._location_params(query, anchor, references=references)
        return self._location_response(
            query, method, params, deadline=deadline, references=references
        )

    def _location_response(
        self,
        query: DocumentQuery,
        method: str,
        params: dict[str, object],
        *,
        deadline: float,
        references: bool,
    ) -> ProviderLocations:
        """Ask, then publish only if the workspace has not moved either side of it."""
        if not self._query_still_current(query):
            return ProviderLocations((), "not_ready", True)
        result = query.process.request(method, params, deadline=deadline)
        locations, filtered = self._normalize_locations(result)
        response = ProviderLocations(
            locations, "provider_reported", references or filtered
        )
        if not self._query_still_current(query):
            return ProviderLocations((), "not_ready", True)
        return response

    def _location_feature(
        self,
        anchor: SourceAnchor,
        *,
        capability: str,
        method: str,
        deadline: float,
        references: bool = False,
    ) -> ProviderLocations:
        if not isinstance(anchor, SourceAnchor):
            raise TypeError("anchor must be a SourceAnchor")
        deadline = validated_deadline(deadline)
        if time.monotonic() >= deadline:
            raise TimeoutError("Pyright semantic request deadline expired")
        with self._operation():
            return self._location_feature_within_operation(
                anchor,
                capability=capability,
                method=method,
                deadline=deadline,
                references=references,
            )

    def definition(
        self,
        anchor: SourceAnchor,
        *,
        deadline: float,
    ) -> ProviderLocations:
        return self._location_feature(
            anchor,
            capability="definition",
            method="textDocument/definition",
            deadline=deadline,
        )

    def references(
        self,
        anchor: SourceAnchor,
        *,
        deadline: float,
    ) -> ProviderLocations:
        return self._location_feature(
            anchor,
            capability="references",
            method="textDocument/references",
            deadline=deadline,
            references=True,
        )

    def implementations(
        self,
        anchor: SourceAnchor,
        *,
        deadline: float,
    ) -> ProviderLocations:
        return self._location_feature(
            anchor,
            capability="implementations",
            method="textDocument/implementation",
            deadline=deadline,
        )

    def type_definition(
        self,
        anchor: SourceAnchor,
        *,
        deadline: float,
    ) -> ProviderLocations:
        return self._location_feature(
            anchor,
            capability="type_definition",
            method="textDocument/typeDefinition",
            deadline=deadline,
        )

    def _symbol_location(
        self, value: Mapping[str, object], uri: str
    ) -> LspLocation | None:
        """Where the symbol is: an explicit location, or its selection range."""
        if "location" in value:
            return self._normalize_location(value.get("location"))
        return _contained_selection(
            lsp_range(value.get("range")),
            lsp_range(value.get("selectionRange")),
            uri,
        )

    def _visit_symbol(self, walk: _SymbolWalk, uri: str) -> None:
        """Take one node off the walk and collect the location it names."""
        value = walk.stack.pop()
        walk.visited += 1
        if not isinstance(value, dict) or not walk.first_visit(value):
            walk.drop()
            return
        _push_symbol_children(walk, value)
        self._collect_symbol(walk, value, uri)

    def _collect_symbol(
        self, walk: _SymbolWalk, value: Mapping[str, object], uri: str
    ) -> None:
        """Add the location this node names, or record that it could not be read."""
        if not _symbol_fields_ok(value):
            walk.drop()
            return
        location = self._symbol_location(value, uri)
        if location is None:
            walk.drop()
            return
        walk.add(location)

    def _walk_document_symbols(self, walk: _SymbolWalk, uri: str) -> None:
        """Visit up to the bound, then record whatever is left unvisited."""
        while walk.stack and walk.visited < MAX_LOCATIONS:
            self._visit_symbol(walk, uri)
        if walk.stack:
            walk.drop()

    def _normalize_document_symbols(
        self,
        result: object,
        uri: str,
    ) -> tuple[tuple[LspLocation, ...], bool]:
        if result is None:
            return (), False
        if not isinstance(result, list):
            return (), True
        walk = _SymbolWalk(
            stack=list(reversed(result[:MAX_LOCATIONS])),
            partial=len(result) > MAX_LOCATIONS,
        )
        self._walk_document_symbols(walk, uri)
        return tuple(walk.locations), walk.partial

    def _document_symbols_within_operation(
        self, path: str, *, deadline: float
    ) -> ProviderLocations:
        query, status = self._begin_document_query(
            "document_symbols", path, deadline=deadline
        )
        if query is None:
            return ProviderLocations((), status, True)
        result = query.process.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": query.document.source.uri}},
            deadline=deadline,
        )
        locations, partial = self._normalize_document_symbols(
            result, query.document.source.uri
        )
        response = ProviderLocations(locations, "provider_reported", partial)
        if not self._query_still_current(query):
            return ProviderLocations((), "not_ready", True)
        return response

    def document_symbols(self, path: str, *, deadline: float) -> ProviderLocations:
        deadline = validated_deadline(deadline)
        with self._operation():
            return self._document_symbols_within_operation(path, deadline=deadline)

    def _anchor_position(
        self, query: DocumentQuery, anchor: SourceAnchor
    ) -> LspPosition:
        """The anchor as a position in the document this query is bound to."""
        source_document = SourceDocument.from_bytes(
            query.document.source.relative_path,
            query.document.content,
        )
        return source_document.to_lsp(anchor, query.encoding)

    def _begin_document_query(
        self, capability: str, path: str, *, deadline: float
    ) -> tuple[DocumentQuery | None, str]:
        """The bound query for this path; the status matters only when it is None."""
        epoch = self._semantic_query_epoch()
        if epoch is None:
            return None, "not_ready"
        self.start(deadline=deadline)
        status = self._capability_status(capability, epoch)
        if status is not None:
            return None, status
        document = self.open_document(path, deadline=deadline)
        # The server begins loading the project on didOpen, so the wait has to
        # come after it, not after `start`.
        self._await_progress_gate(deadline)
        self._refresh_readiness_after_progress(deadline)
        return self._document_query(document, epoch), "not_ready"

    def _refresh_readiness_after_progress(self, deadline: float) -> None:
        """Recompute readiness once the project load the query waited on is done.

        Readiness is settled while the document is opened, which for a
        progress-gated server is necessarily *before* the project load ends --
        `_probe_document` runs during `didOpen`, and `_await_progress_gate` is
        what comes after. Without this the envelope reported
        `protocol_initialized` for an answer whose query had in fact waited for
        `workDoneProgress/end`, so the field an agent reads to decide whether to
        trust the answer understated it. Measured on the TypeScript fixture: the
        first query of a session reported `protocol_initialized`, later ones
        `query_ready`, for identical and correct answers.

        Confined to profiles that gate on progress. A profile that does not gate
        cannot have changed readiness during a wait it never performed, so
        Pyright takes no extra `promote_workspace_ready` round trip.
        """
        if not self._profile.gates_on_progress():
            return
        with self._lock:
            self._refresh_readiness_locked(deadline=deadline)

    def _hover_within_operation(
        self, anchor: SourceAnchor, *, deadline: float
    ) -> ProviderHover:
        query, _status = self._begin_document_query(
            "hover", anchor.path, deadline=deadline
        )
        if query is None:
            return ProviderHover(None, None, True)
        position = self._anchor_position(query, anchor)
        return self._hover_response(query, position, deadline=deadline)

    def _hover_response(
        self, query: DocumentQuery, position: LspPosition, *, deadline: float
    ) -> ProviderHover:
        if not self._query_still_current(query):
            return ProviderHover(None, None, True)
        result = query.process.request(
            "textDocument/hover",
            position_params(query, position),
            deadline=deadline,
        )
        response = _hover_response(result)
        if not self._query_still_current(query):
            return ProviderHover(None, None, True)
        return response

    def hover(self, anchor: SourceAnchor, *, deadline: float) -> ProviderHover:
        if not isinstance(anchor, SourceAnchor):
            raise TypeError("anchor must be a SourceAnchor")
        deadline = validated_deadline(deadline)
        with self._operation():
            return self._hover_within_operation(anchor, deadline=deadline)

    def _workspace_state(self, capability: str, epoch: int) -> _WorkspaceState:
        with self._lock:
            return _WorkspaceState(
                process=self._process,
                generation=self._generation_nonce,
                readiness=self._readiness,
                supported=self._capabilities.get(capability, False),
                initialized=self._position_encoding is not None,
                current=self._semantic_query_epoch_current_locked(epoch),
            )

    def _workspace_query_current(self, query: _WorkspaceQuery) -> bool:
        """Whether the session still stands where the workspace query began."""
        with self._lock:
            return (
                self._process is query.process
                and self._generation_nonce == query.generation
                and self._readiness == query.readiness
                and self._semantic_query_epoch_current_locked(query.epoch)
            )

    def _workspace_symbol_response(self, result: object) -> ProviderLocations:
        """The locations the server reported for a workspace symbol query."""
        if result is None:
            return ProviderLocations((), "provider_reported", False)
        if not isinstance(result, list):
            return ProviderLocations((), "provider_reported", True)
        values, partial = _symbol_locations_payload(result)
        locations, filtered = self._normalize_locations(values)
        return ProviderLocations(locations, "provider_reported", partial or filtered)

    def _workspace_symbols_within_operation(
        self, query: str, *, deadline: float
    ) -> ProviderLocations:
        epoch = self._semantic_query_epoch()
        if epoch is None:
            return ProviderLocations((), "not_ready", True)
        self.start(deadline=deadline)
        state = self._workspace_state("workspace_symbols", epoch)
        status = _workspace_query_status(state)
        if status is not None:
            return ProviderLocations((), status, True)
        bound = _WorkspaceQuery(
            state.process, state.generation, state.readiness, epoch
        )
        return self._workspace_symbol_result(bound, query, deadline=deadline)

    def _workspace_symbol_result(
        self, bound: _WorkspaceQuery, query: str, *, deadline: float
    ) -> ProviderLocations:
        result = bound.process.request(
            "workspace/symbol", {"query": query}, deadline=deadline
        )
        response = self._workspace_symbol_response(result)
        if not self._workspace_query_current(bound):
            return ProviderLocations((), "not_ready", True)
        return response

    def workspace_symbols(
        self,
        query: str,
        *,
        deadline: float,
    ) -> ProviderLocations:
        _check_symbol_query(query)
        deadline = validated_deadline(deadline)
        with self._operation():
            return self._workspace_symbols_within_operation(query, deadline=deadline)

    def _semantic_query_epoch(self) -> int | None:
        with self._lock:
            return self._semantic_query_epoch_locked()

    def _query_ready_locked(
        self,
        process: LspProcess | None,
        generation: str | None,
        document: OpenDocument,
        epoch: int,
    ) -> bool:
        if process is None or generation is None:
            return False
        return self._document_query_current_locked(
            process, generation, document, epoch
        )

    def _query_still_current(self, query: DocumentQuery) -> bool:
        """Whether the query still refers to the workspace it started on."""
        with self._lock:
            return self._document_query_current_locked(
                query.process, query.generation, query.document, query.epoch
            )

    def _capability_status(self, capability: str, epoch: int) -> str | None:
        """A refusal status for this capability, or None when it may proceed."""
        with self._lock:
            process = self._process
            encoding = self._position_encoding
            supported = self._capabilities.get(capability, False)
            current = self._semantic_query_epoch_current_locked(epoch)
        if process is None or encoding is None or not current:
            return "not_ready"
        if not supported:
            return "unsupported"
        return None

    def _document_query(self, document: OpenDocument, epoch: int) -> DocumentQuery | None:
        """The bound query, or None when the workspace moved under it."""
        with self._lock:
            encoding = self._position_encoding
            process = self._process
            generation = self._generation_nonce
            ready = self._query_ready_locked(process, generation, document, epoch)
        if not ready or encoding is None or process is None or generation is None:
            return None
        return DocumentQuery(process, generation, document, epoch, encoding)
