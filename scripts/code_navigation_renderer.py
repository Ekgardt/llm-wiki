"""Deterministic compact rendering for normalized navigation results."""

from __future__ import annotations

import json
from collections import defaultdict

from code_navigation import (
    NavigationDiagnostic,
    NavigationLocation,
    NavigationResult,
    NavigationStatus,
)

DEFAULT_LIMIT = 10
MAX_LIMIT = 100
MAX_ESTIMATED_TOKENS = 1_200
_HOVER_BYTE_CEILING = 2048
_SIGNATURE_BYTE_CEILING = 1024

_NON_PARTIAL = frozenset({NavigationStatus.OK})


def estimate_tokens(text: str) -> int:
    return (len(text.encode("utf-8")) + 3) // 4


def _coerce_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer or None")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError("limit must be between 1 and MAX_LIMIT")
    return limit


def _coerce_offset(offset: int | None, total: int) -> int:
    if offset is None:
        return 0
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise TypeError("offset must be an integer or None")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    return min(offset, total)


def _strip_signature(signature: str | None) -> tuple[str | None, bool]:
    if signature is None:
        return None, False
    encoded = signature.encode("utf-8", errors="strict")
    if len(encoded) <= _SIGNATURE_BYTE_CEILING:
        first_line = signature.split("\n", 1)[0].rstrip()
        if first_line != signature.rstrip():
            return first_line, True
        return first_line, False
    first_line = signature.split("\n", 1)[0].rstrip()
    if len(first_line.encode("utf-8", errors="strict")) > _SIGNATURE_BYTE_CEILING:
        truncated = first_line.encode("utf-8", errors="strict")[:_SIGNATURE_BYTE_CEILING].decode(
            "utf-8", errors="ignore"
        )
        return truncated, True
    return first_line, True


def _bound_hover(hover: str | None) -> tuple[str | None, bool]:
    if hover is None:
        return None, False
    encoded = hover.encode("utf-8", errors="strict")
    if len(encoded) <= _HOVER_BYTE_CEILING:
        return hover, False
    bounded = encoded[:_HOVER_BYTE_CEILING].decode("utf-8", errors="ignore")
    return bounded, True


def _location_sort_key(location: NavigationLocation) -> tuple[object, ...]:
    resolution_order = {
        "lsp_confirmed": 0,
        "lsp_and_graph": 1,
        "graph_confirmed": 2,
        "lsp_only": 3,
        "graph_candidate": 4,
        "ambiguous": 5,
        "unresolved": 6,
        "unsupported": 7,
    }
    return (
        location.range.byte_start,
        location.range.byte_end,
        resolution_order.get(location.resolution.value, 8),
    )


def _location_view(location: NavigationLocation) -> dict[str, object]:
    signature, _sig_truncated = _strip_signature(location.signature)
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
    }


def _group_key(location: NavigationLocation) -> tuple[str, str]:
    return (location.path, location.containing_symbol or "")


def _build_groups(
    locations: tuple[NavigationLocation, ...],
) -> list[dict[str, object]]:
    buckets: dict[tuple[str, str], list[NavigationLocation]] = defaultdict(list)
    for location in locations:
        buckets[_group_key(location)].append(location)
    groups: list[dict[str, object]] = []
    for (path, symbol), members in sorted(buckets.items()):
        members.sort(key=_location_sort_key)
        groups.append(
            {
                "path": path,
                "containing_symbol": symbol or None,
                "locations": [_location_view(member) for member in members],
            }
        )
    return groups


def _diagnostic_view(diagnostic: NavigationDiagnostic) -> dict[str, object]:
    return {
        "path": diagnostic.path,
        "range": {
            "byte_start": diagnostic.range.byte_start,
            "byte_end": diagnostic.range.byte_end,
        },
        "severity": diagnostic.severity,
        "code": diagnostic.code,
        "message": diagnostic.message,
    }


def _provenance_view(provenance: tuple) -> list[dict[str, str]]:
    return [
        {
            "source": item.source,
            "provider": item.provider,
            "version": item.version,
            "observation": item.observation,
        }
        for item in provenance
    ]


def _encode_payload(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


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
    total = result.total
    effective_offset = _coerce_offset(offset, total)
    effective_limit = _coerce_limit(limit)
    warnings: list[str] = list(result.warnings)
    hover, hover_truncated = _bound_hover(result.hover)
    if hover_truncated:
        warnings.append("hover_truncated")
    all_locations = tuple(result.locations)
    available = max(0, len(all_locations) - effective_offset)
    window = min(effective_limit, available)
    window = max(0, window)
    while window > 0:
        page = all_locations[effective_offset : effective_offset + window]
        groups = _build_groups(page)
        payload = _assemble(
            result,
            groups=groups,
            offset=effective_offset,
            limit=effective_limit,
            window=window,
            total=total,
            hover=hover,
            warnings=warnings,
            truncated=window < available,
            omitted=max(0, available - window),
        )
        if estimate_tokens(_encode_payload(payload)) <= MAX_ESTIMATED_TOKENS:
            return payload
        window -= 1
    payload = _assemble(
        result,
        groups=[],
        offset=effective_offset,
        limit=effective_limit,
        window=0,
        total=total,
        hover=hover,
        warnings=[*warnings, "output_token_bound"],
        truncated=available > 0,
        omitted=available,
    )
    return payload


def _assemble(
    result: NavigationResult,
    *,
    groups: list[dict[str, object]],
    offset: int,
    limit: int,
    window: int,
    total: int,
    hover: str | None,
    warnings: list[str],
    truncated: bool,
    omitted: int,
) -> dict[str, object]:
    next_offset = offset + window if (offset + window) < len(result.locations) else None
    ordered: dict[str, object] = {}
    ordered["status"] = result.status.value
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
    ordered["diagnostics"] = [_diagnostic_view(d) for d in result.diagnostics]
    ordered["hover"] = hover
    ordered["provenance"] = _provenance_view(result.provenance)
    ordered["warnings"] = tuple(warnings)
    return ordered
