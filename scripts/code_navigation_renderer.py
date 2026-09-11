"""Deterministic compact rendering for normalized navigation results."""

from __future__ import annotations

import json
from collections import defaultdict

from code_intelligence import Capability
from code_navigation import (
    NavigationDiagnostic,
    NavigationLocation,
    NavigationResult,
    NavigationStatus,
    Provenance,
)

DEFAULT_LIMIT = 10
MAX_LIMIT = 100
MAX_ESTIMATED_TOKENS = 1_200
_HOVER_BYTE_CEILING = 2048
_MAX_JSON_SAFE_INTEGER = 2**53 - 1
_SIGNATURE_BYTE_CEILING = 1024

_RESOLUTION_ORDER = {
    "lsp_confirmed": 0,
    "lsp_and_graph": 1,
    "graph_confirmed": 2,
    "lsp_only": 3,
    "graph_candidate": 4,
    "ambiguous": 5,
    "unresolved": 6,
    "unsupported": 7,
}


def estimate_tokens(text: str) -> int:
    return (len(text.encode("utf-8")) + 3) // 4


def _coerce_limit(limit: int | None) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError("limit must be between 1 and MAX_LIMIT")
    return limit


def _coerce_offset(offset: int | None) -> int:
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise TypeError("offset must be an integer")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    _require_json_safe_integer(offset, "offset")
    return offset


def _require_json_safe_integer(value: int, label: str) -> None:
    if value > _MAX_JSON_SAFE_INTEGER:
        raise ValueError(f"{label} must be a JSON-safe integer")


def _validate_location_integers(location: NavigationLocation) -> None:
    _require_json_safe_integer(location.line, "location line")
    _require_json_safe_integer(location.character, "location character")
    _require_json_safe_integer(location.range.byte_start, "location byte_start")
    _require_json_safe_integer(location.range.byte_end, "location byte_end")


def _validate_result_integers(result: NavigationResult) -> None:
    _require_json_safe_integer(result.total, "total")
    if result.document_version is not None:
        _require_json_safe_integer(result.document_version, "document_version")
    for location in result.locations:
        _validate_location_integers(location)
    for diagnostic in result.diagnostics:
        _require_json_safe_integer(
            diagnostic.range.byte_start,
            "diagnostic byte_start",
        )
        _require_json_safe_integer(
            diagnostic.range.byte_end,
            "diagnostic byte_end",
        )
        for location in diagnostic.related:
            _validate_location_integers(location)


def _utf8_prefix(value: str, ceiling: int) -> str:
    encoded = value.encode("utf-8", errors="strict")
    if len(encoded) <= ceiling:
        return value
    prefix = encoded[:ceiling]
    try:
        return prefix.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        return prefix[: exc.start].decode("utf-8", errors="strict")


def _strip_signature(
    signature: str | None,
) -> tuple[str | None, bool, int | None]:
    if signature is None:
        return None, False, None
    encoded = signature.encode("utf-8", errors="strict")
    lines = signature.splitlines()
    first_line = lines[0] if lines else ""
    bounded = _utf8_prefix(first_line, _SIGNATURE_BYTE_CEILING)
    truncated = bounded != signature
    return bounded, truncated, len(encoded) if truncated else None


def _bound_hover(hover: str | None) -> tuple[str | None, bool, int | None]:
    if hover is None:
        return None, False, None
    encoded = hover.encode("utf-8", errors="strict")
    if len(encoded) <= _HOVER_BYTE_CEILING:
        return hover, False, None
    return _utf8_prefix(hover, _HOVER_BYTE_CEILING), True, len(encoded)


def _optional_text_key(value: str | None) -> tuple[int, str]:
    return (0, "") if value is None else (1, value)


def _provenance_key(value: Provenance) -> tuple[str, str, str, str]:
    return (value.source, value.provider, value.version, value.observation)


def _location_sort_key(location: NavigationLocation) -> tuple[object, ...]:
    return (
        location.path,
        _optional_text_key(location.containing_symbol),
        location.range.byte_start,
        location.range.byte_end,
        _RESOLUTION_ORDER.get(location.resolution.value, 8),
        location.resolution.value,
        location.line,
        location.character,
        _optional_text_key(location.signature),
        tuple(sorted(_provenance_key(item) for item in location.provenance)),
    )


def _location_view(
    location: NavigationLocation,
) -> tuple[dict[str, object], tuple[str, ...]]:
    signature, signature_truncated, original_bytes = _strip_signature(
        location.signature
    )
    warnings: tuple[str, ...] = ()
    if signature_truncated:
        warnings = (
            "signature_truncated",
            f"signature_original_bytes:{original_bytes}",
        )
    return {
        "path": location.path,
        "line": location.line,
        "character": location.character,
        "range": {
            "byte_start": location.range.byte_start,
            "byte_end": location.range.byte_end,
        },
        "containing_symbol": location.containing_symbol,
        "signature": signature,
        "resolution": location.resolution.value,
    }, warnings


def _group_key(location: NavigationLocation) -> tuple[str, int, str]:
    tag, symbol = _optional_text_key(location.containing_symbol)
    return location.path, tag, symbol


def _build_groups(
    locations: tuple[NavigationLocation, ...],
) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    buckets: dict[tuple[str, int, str], list[NavigationLocation]] = defaultdict(list)
    for location in locations:
        buckets[_group_key(location)].append(location)
    groups: list[dict[str, object]] = []
    warnings: list[str] = []
    for (path, symbol_tag, symbol), members in sorted(buckets.items()):
        members.sort(key=_location_sort_key)
        views: list[dict[str, object]] = []
        for member in members:
            view, view_warnings = _location_view(member)
            views.append(view)
            warnings.extend(view_warnings)
        groups.append(
            {
                "path": path,
                "containing_symbol": None if symbol_tag == 0 else symbol,
                "locations": views,
            }
        )
    return groups, tuple(warnings)


def _diagnostic_sort_key(diagnostic: NavigationDiagnostic) -> tuple[object, ...]:
    return (
        diagnostic.path,
        diagnostic.range.byte_start,
        diagnostic.range.byte_end,
        diagnostic.severity.value,
        _optional_text_key(diagnostic.code),
        diagnostic.message,
        tuple(
            _location_sort_key(location)
            for location in sorted(diagnostic.related, key=_location_sort_key)
        ),
        tuple(sorted(_provenance_key(item) for item in diagnostic.provenance)),
    )


def _diagnostic_view(
    diagnostic: NavigationDiagnostic,
) -> tuple[dict[str, object], tuple[str, ...]]:
    related: list[dict[str, object]] = []
    warnings: list[str] = []
    for location in sorted(diagnostic.related, key=_location_sort_key):
        view, view_warnings = _location_view(location)
        related.append(view)
        warnings.extend(view_warnings)
    return {
        "path": diagnostic.path,
        "range": {
            "byte_start": diagnostic.range.byte_start,
            "byte_end": diagnostic.range.byte_end,
        },
        "severity": diagnostic.severity.value,
        "code": diagnostic.code,
        "message": diagnostic.message,
        "related": related,
    }, tuple(warnings)


def _build_diagnostics(
    diagnostics: tuple[NavigationDiagnostic, ...],
) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    views: list[dict[str, object]] = []
    warnings: list[str] = []
    for diagnostic in diagnostics:
        view, view_warnings = _diagnostic_view(diagnostic)
        views.append(view)
        warnings.extend(view_warnings)
    return views, tuple(warnings)


def _provenance_view(
    provenance: tuple[Provenance, ...],
) -> list[dict[str, str]]:
    return [
        {
            "source": item.source,
            "provider": item.provider,
            "version": item.version,
            "observation": item.observation,
        }
        for item in sorted(provenance, key=_provenance_key)
    ]


def _warnings(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({warning for group in groups for warning in group}))


def _encode_payload(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _render_status(status: NavigationStatus, *, partial: bool) -> NavigationStatus:
    if status is NavigationStatus.OK and partial:
        return NavigationStatus.PARTIAL
    return status


def render_navigation(
    result: NavigationResult,
    *,
    offset: int | None = None,
    limit: int | None = None,
    include_source: bool = False,
) -> dict[str, object]:
    if not isinstance(result, NavigationResult):
        raise TypeError("result must be a NavigationResult")
    if not isinstance(include_source, bool):
        raise TypeError("include_source must be a boolean")
    page = _NavigationPage(
        result,
        _coerce_offset(result.offset if offset is None else offset),
        _coerce_limit(result.limit if limit is None else limit),
    )
    return page.fitted()


def _require_fact_shape(result: NavigationResult, diagnostic_mode: bool) -> None:
    stray = result.locations if diagnostic_mode else result.diagnostics
    if stray:
        raise ValueError("navigation result fact shape is invalid")


def _require_total(result: NavigationResult, fact_count: int) -> None:
    if result.total != fact_count:
        raise ValueError("result total must equal primary fact count")


def _sorted_facts(
    result: NavigationResult, diagnostic_mode: bool
) -> tuple[tuple[NavigationDiagnostic, ...], tuple[NavigationLocation, ...], int]:
    """(diagnostics, locations, primary fact count), the primary facts in render order."""
    if diagnostic_mode:
        _require_total(result, len(result.diagnostics))
        return tuple(sorted(result.diagnostics, key=_diagnostic_sort_key)), (), len(result.diagnostics)
    _require_total(result, len(result.locations))
    return (), tuple(sorted(result.locations, key=_location_sort_key)), len(result.locations)


def _base_warnings(result: NavigationResult, hover_truncated: bool, hover_original_bytes: int) -> tuple[str, ...]:
    base = tuple(result.warnings)
    if not hover_truncated:
        return base
    return (*base, "hover_truncated", f"hover_original_bytes:{hover_original_bytes}")


def _token_warnings(token_reduced: bool, fact_omitted: bool) -> tuple[str, ...]:
    warnings: tuple[str, ...] = ("output_token_bound",) if token_reduced else ()
    if fact_omitted:
        warnings = (*warnings, "fact_omitted_token_bound")
    return warnings


def _fits(payload: dict[str, object]) -> bool:
    return estimate_tokens(_encode_payload(payload)) <= MAX_ESTIMATED_TOKENS


def _within_token_bound(payload: dict[str, object]) -> dict[str, object]:
    if not _fits(payload):
        raise ValueError("navigation metadata exceeds token bound")
    return payload


class _NavigationPage:
    """One result's facts, rendered at the widest window that fits the token bound."""

    def __init__(self, result: NavigationResult, offset: int, limit: int) -> None:
        self.result = result
        self.total = result.total
        self.offset = offset
        self.limit = limit
        self.diagnostic_mode = result.requested_capability is Capability.DIAGNOSTICS
        _require_fact_shape(result, self.diagnostic_mode)
        _validate_result_integers(result)
        self.diagnostics, self.locations, fact_count = _sorted_facts(result, self.diagnostic_mode)
        self.hover, self.hover_truncated, hover_original_bytes = _bound_hover(result.hover)
        self.base_warnings = _base_warnings(result, self.hover_truncated, hover_original_bytes)
        self.requested_window = min(limit, max(0, fact_count - offset))

    def fitted(self) -> dict[str, object]:
        if self.requested_window == 0:
            return _within_token_bound(self.trial(0, token_reduced=False))
        _within_token_bound(self.trial(0, token_reduced=True))
        full_payload = self.trial(self.requested_window, token_reduced=False)
        if _fits(full_payload):
            return full_payload
        return self._largest_fitting_page()

    def _largest_fitting_page(self) -> dict[str, object]:
        best_payload = self._widest_fitting_window(1, self.requested_window - 1)
        if best_payload is not None:
            return best_payload
        return _within_token_bound(self.trial(0, token_reduced=True, fact_omitted=True))

    def _widest_fitting_window(self, low: int, high: int) -> dict[str, object] | None:
        """Binary search over reduced windows; the widest payload that fits, if any."""
        best_payload: dict[str, object] | None = None
        while low <= high:
            window = (low + high) // 2
            payload = self.trial(window, token_reduced=True)
            if _fits(payload):
                best_payload = payload
                low = window + 1
            else:
                high = window - 1
        return best_payload

    def _page_views(self, window: int) -> tuple[list[dict[str, object]], list[dict[str, object]], tuple]:
        end = self.offset + window
        if self.diagnostic_mode:
            diagnostic_views, clipping_warnings = _build_diagnostics(self.diagnostics[self.offset : end])
            return [], diagnostic_views, clipping_warnings
        groups, clipping_warnings = _build_groups(self.locations[self.offset : end])
        return groups, [], clipping_warnings

    def _truncated(self, omitted: int, token_reduced: bool, clipping_warnings) -> bool:
        return omitted > 0 or token_reduced or self.hover_truncated or bool(clipping_warnings)

    def trial(
        self,
        window: int,
        *,
        token_reduced: bool,
        fact_omitted: bool = False,
    ) -> dict[str, object]:
        groups, diagnostic_views, clipping_warnings = self._page_views(window)
        consumed = 1 if fact_omitted else window
        rendered_warnings = _warnings(
            self.base_warnings,
            clipping_warnings,
            _token_warnings(token_reduced, fact_omitted),
        )
        omitted = int(fact_omitted) + max(0, self.total - (self.offset + consumed))
        truncated = self._truncated(omitted, token_reduced, clipping_warnings)
        return _assemble(
            self.result,
            status=_render_status(self.result.status, partial=truncated),
            groups=groups,
            diagnostics=diagnostic_views,
            offset=self.offset,
            limit=self.limit,
            consumed=consumed,
            total=self.total,
            hover=self.hover,
            warnings=rendered_warnings,
            truncated=truncated,
            omitted=omitted,
        )


def _next_offset(offset: int, consumed: int, total: int) -> int | None:
    end = offset + consumed
    if consumed > 0 and end < total:
        return end
    return None


def _assemble(
    result: NavigationResult,
    *,
    status: NavigationStatus,
    groups: list[dict[str, object]],
    diagnostics: list[dict[str, object]],
    offset: int,
    limit: int,
    consumed: int,
    total: int,
    hover: str | None,
    warnings: tuple[str, ...],
    truncated: bool,
    omitted: int,
) -> dict[str, object]:
    next_offset = _next_offset(offset, consumed, total)
    ordered: dict[str, object] = {}
    ordered["status"] = status.value
    ordered["freshness"] = {
        "workspace_revision_before": result.workspace_revision_before,
        "workspace_revision_after": result.workspace_revision_after,
        "current": result.workspace_revision_after,
    }
    ordered["provider"] = {
        "name": result.provider,
        "version": result.provider_version,
    }
    ordered["symbol"] = result.symbol
    ordered["total"] = total
    ordered["requested_capability"] = result.requested_capability.value
    ordered["effective_capability"] = (
        result.effective_capability.value
        if result.effective_capability is not None
        else None
    )
    ordered["position_encoding"] = (
        result.position_encoding.value
        if result.position_encoding is not None
        else None
    )
    ordered["readiness"] = result.readiness
    ordered["repository"] = {
        "repository_id": result.repository_id,
        "checkout_id": result.checkout_id,
    }
    ordered["document_version"] = result.document_version
    ordered["offset"] = offset
    ordered["limit"] = limit
    ordered["truncated"] = truncated
    ordered["omitted"] = omitted
    ordered["next_offset"] = next_offset
    ordered["resolution"] = result.resolution.value
    ordered["groups"] = groups
    ordered["diagnostics"] = diagnostics
    ordered["hover"] = hover
    ordered["provenance"] = _provenance_view(result.provenance)
    ordered["warnings"] = warnings
    return ordered
