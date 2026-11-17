"""Call-hierarchy queries: prepare one item, then read its bounded incoming or outgoing calls."""

from collections.abc import Mapping
from types import MappingProxyType

from llm_wiki.pyright_session_queries import DocumentQuery, position_params
from llm_wiki.pyright_session_ranges import (
    MAX_CALL_ITEM_TEXT_BYTES,
    BoundedLocations,
    bounded_text,
    call_item_range_value,
    lsp_coordinate,
    lsp_range,
    range_contains,
    range_json,
    symbol_tags_ok,
)
from llm_wiki.pyright_session_values import (
    LspLocation,
    ProviderCalls,
    validated_deadline,
)
from lsp_positions import LspRange, SourceAnchor
from lsp_protocol import MAX_LOCATIONS
from lsp_security import normalize_provider_uri

_MAX_PREPARED_CALL_ITEMS = MAX_LOCATIONS


_CALL_METHODS = MappingProxyType(
    {
        "incoming": "callHierarchy/incomingCalls",
        "outgoing": "callHierarchy/outgoingCalls",
    }
)


_CALL_RESULT_KEYS = MappingProxyType({"incoming": "from", "outgoing": "to"})


def _usable_kind(kind: int | None) -> bool:
    """A symbol kind the protocol actually assigns."""
    return kind is not None and kind != 0


def _call_item_ranges(
    value: Mapping[str, object],
) -> tuple[LspRange, LspRange] | None:
    """The item's range and selection range, when the selection sits inside it."""
    range_ = lsp_range(value.get("range"))
    selection = lsp_range(value.get("selectionRange"))
    if range_ is None or selection is None:
        return None
    if not range_contains(range_, selection):
        return None
    return range_, selection


def _call_item_core(
    value: Mapping[str, object],
) -> tuple[str, int, str, LspRange, LspRange] | None:
    """The required fields of a call item, when all are present and consistent."""
    name = bounded_text(value.get("name"), MAX_CALL_ITEM_TEXT_BYTES)
    kind = lsp_coordinate(value.get("kind"))
    uri = value.get("uri")
    if not name or not _usable_kind(kind) or not isinstance(uri, str):
        return None
    ranges = _call_item_ranges(value)
    if ranges is None:
        return None
    return name, kind, uri, ranges[0], ranges[1]


def _call_item_detail(
    value: Mapping[str, object], item: dict[str, object]
) -> bool:
    """Copy the optional detail; False when it is present but unusable."""
    detail = value.get("detail")
    if detail is None:
        return True
    bounded = bounded_text(detail, MAX_CALL_ITEM_TEXT_BYTES)
    if bounded is None:
        return False
    item["detail"] = bounded
    return True


def _call_item_tags(value: Mapping[str, object], item: dict[str, object]) -> bool:
    """Copy the optional tags; False when they are present but unusable."""
    tags = value.get("tags")
    if tags is None:
        return True
    if not symbol_tags_ok(tags):
        return False
    item["tags"] = list(tags)
    return True


def _call_item_optionals(
    value: Mapping[str, object], item: dict[str, object]
) -> bool:
    """Copy detail, tags and opaque data; False when one is present but unusable."""
    if not _call_item_detail(value, item):
        return False
    if not _call_item_tags(value, item):
        return False
    return _call_item_data(value, item)


def _call_item_data(value: Mapping[str, object], item: dict[str, object]) -> bool:
    """The opaque round-trip field, copied through untouched when present."""
    if "data" in value:
        item["data"] = value["data"]
    return True


class SessionCalls:
    """Answer incoming and outgoing call-hierarchy queries."""

    def _sanitize_call_item(self, value: object) -> dict[str, object] | None:
        if not isinstance(value, dict):
            return None
        core = _call_item_core(value)
        if core is None:
            return None
        return self._call_item_from_core(value, core)

    def _call_item_from_core(
        self, value: Mapping[str, object], core: tuple
    ) -> dict[str, object] | None:
        name, kind, uri, range_, selection = core
        source = normalize_provider_uri(self._repository, uri)
        if source is None:
            return None
        item: dict[str, object] = {
            "name": name,
            "kind": kind,
            "uri": source.uri,
            "range": range_json(range_),
            "selectionRange": range_json(selection),
        }
        return item if _call_item_optionals(value, item) else None

    def _call_location(self, value: object) -> LspLocation | None:
        if not isinstance(value, dict):
            return None
        return self._located_at(value.get("uri"), call_item_range_value(value))

    def _prepare_call_hierarchy(
        self, anchor: SourceAnchor, query: DocumentQuery, *, deadline: float
    ) -> tuple[bool, object]:
        """Whether the query held, and what the server prepared for the anchor."""
        position = self._anchor_position(query, anchor)
        if not self._query_still_current(query):
            return False, None
        prepared = query.process.request(
            "textDocument/prepareCallHierarchy",
            position_params(query, position),
            deadline=deadline,
        )
        if not self._query_still_current(query):
            return False, None
        return True, prepared

    def _sanitized_call_items(self, prepared: list[object]) -> list[object]:
        items: list[object] = []
        for value in prepared[:_MAX_PREPARED_CALL_ITEMS]:
            item = self._sanitize_call_item(value)
            if item is not None:
                items.append(item)
        return items

    def _call_result(
        self, item: object, query: DocumentQuery, *, method: str, deadline: float
    ) -> tuple[bool, object]:
        """Whether the query held across the request, and what came back."""
        if not self._query_still_current(query):
            return False, None
        result = query.process.request(method, {"item": item}, deadline=deadline)
        if not self._query_still_current(query):
            return False, None
        return True, result

    def _call_entry_location(
        self, call: object, result_key: str
    ) -> LspLocation | None:
        if not isinstance(call, dict):
            return None
        return self._call_location(call.get(result_key))

    def _collect_call_locations(
        self, result: object, result_key: str, collected: BoundedLocations
    ) -> None:
        if not isinstance(result, list):
            return
        for call in result[:MAX_LOCATIONS]:
            location = self._call_entry_location(call, result_key)
            if location is not None:
                collected.add(location)

    def _call_locations(
        self,
        items: list[object],
        query: DocumentQuery,
        *,
        direction: str,
        deadline: float,
    ) -> list[LspLocation] | None:
        """Every location the calls name, or None when the query went stale."""
        method = _CALL_METHODS[direction]
        result_key = _CALL_RESULT_KEYS[direction]
        collected = BoundedLocations()
        for item in items:
            current, result = self._call_result(
                item, query, method=method, deadline=deadline
            )
            if not current:
                return None
            self._collect_call_locations(result, result_key, collected)
        if not self._query_still_current(query):
            return None
        return collected.locations

    def _collect_calls(
        self,
        anchor: SourceAnchor,
        query: DocumentQuery,
        *,
        direction: str,
        deadline: float,
    ) -> ProviderCalls:
        current, prepared = self._prepare_call_hierarchy(
            anchor, query, deadline=deadline
        )
        if not current:
            return ProviderCalls(direction, (), "not_ready", True)
        if prepared is None or not isinstance(prepared, list):
            return ProviderCalls(direction, (), "provider_reported", True)
        return self._calls_from_prepared(
            prepared, query, direction=direction, deadline=deadline
        )

    def _calls_from_prepared(
        self,
        prepared: list[object],
        query: DocumentQuery,
        *,
        direction: str,
        deadline: float,
    ) -> ProviderCalls:
        items = self._sanitized_call_items(prepared)
        locations = self._call_locations(
            items, query, direction=direction, deadline=deadline
        )
        if locations is None:
            return ProviderCalls(direction, (), "not_ready", True)
        return ProviderCalls(direction, tuple(locations), "provider_reported", True)

    def _calls_within_operation(
        self, anchor: SourceAnchor, *, direction: str, deadline: float
    ) -> ProviderCalls:
        epoch = self._semantic_query_epoch()
        if epoch is None:
            return ProviderCalls(direction, (), "not_ready", True)
        self.start(deadline=deadline)
        status = self._capability_status("calls", epoch)
        if status is not None:
            return ProviderCalls(direction, (), status, True)
        return self._calls_for_open_document(
            anchor, epoch, direction=direction, deadline=deadline
        )

    def _calls_for_open_document(
        self, anchor: SourceAnchor, epoch: int, *, direction: str, deadline: float
    ) -> ProviderCalls:
        document = self.open_document(anchor.path, deadline=deadline)
        self._await_progress_gate(deadline)
        self._refresh_readiness_after_progress(deadline)
        query = self._document_query(document, epoch)
        if query is None:
            return ProviderCalls(direction, (), "not_ready", True)
        return self._collect_calls(
            anchor, query, direction=direction, deadline=deadline
        )

    def _calls(
        self,
        anchor: SourceAnchor,
        *,
        direction: str,
        deadline: float,
    ) -> ProviderCalls:
        if not isinstance(anchor, SourceAnchor):
            raise TypeError("anchor must be a SourceAnchor")
        deadline = validated_deadline(deadline)
        with self._operation():
            return self._calls_within_operation(
                anchor, direction=direction, deadline=deadline
            )

    def incoming_calls(
        self,
        anchor: SourceAnchor,
        *,
        deadline: float,
    ) -> ProviderCalls:
        return self._calls(anchor, direction="incoming", deadline=deadline)

    def outgoing_calls(
        self,
        anchor: SourceAnchor,
        *,
        deadline: float,
    ) -> ProviderCalls:
        return self._calls(anchor, direction="outgoing", deadline=deadline)
