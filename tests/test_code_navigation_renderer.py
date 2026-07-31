"""Deterministic navigation renderer contract tests."""

from __future__ import annotations

import json

import pytest
from code_intelligence import Capability, PositionEncoding, PositionRange
from code_navigation import (
    NavigationLocation,
    NavigationResult,
    NavigationStatus,
    Provenance,
    ResolutionLabel,
)
from code_navigation_renderer import (
    DEFAULT_LIMIT,
    MAX_ESTIMATED_TOKENS,
    MAX_LIMIT,
    estimate_tokens,
    render_navigation,
)

_PROVENANCE = (Provenance("lsp", "pyright", "1.1.411", "provider_reported"),)


def _location(
    path: str = "pkg/api.py",
    start: int = 0,
    end: int = 4,
    symbol: str | None = "Service",
    signature: str | None = None,
    resolution: ResolutionLabel = ResolutionLabel.LSP_CONFIRMED,
) -> NavigationLocation:
    return NavigationLocation(
        path, PositionRange(start, end), 1, 0, symbol, signature, resolution, _PROVENANCE
    )


def _result(
    count: int = 25,
    *,
    status: NavigationStatus = NavigationStatus.OK,
    resolution: ResolutionLabel = ResolutionLabel.LSP_CONFIRMED,
    hover: str | None = None,
) -> NavigationResult:
    locations = tuple(
        _location(start=i * 10, end=i * 10 + 4) for i in range(count)
    )
    return NavigationResult(
        status,
        Capability.DEFINITIONS,
        Capability.DEFINITIONS,
        "pyright",
        "1.1.411",
        "repo",
        "checkout",
        "abc123",
        "abc123",
        1,
        PositionEncoding.UTF8,
        "query_ready",
        None,
        count,
        0,
        DEFAULT_LIMIT,
        locations,
        (),
        hover,
        resolution,
        _PROVENANCE,
        (),
    )


def test_renderer_constants_are_exact() -> None:
    assert DEFAULT_LIMIT == 10
    assert MAX_LIMIT == 100
    assert MAX_ESTIMATED_TOKENS == 1_200
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abc") == 1


def test_renderer_puts_keys_in_exact_order() -> None:
    rendered = render_navigation(_result(1), offset=0, limit=10)
    expected_order = [
        "status",
        "freshness",
        "provider",
        "symbol",
        "total",
        "requested_capability",
        "effective_capability",
        "position_encoding",
        "readiness",
        "repository",
        "document_version",
        "offset",
        "limit",
        "truncated",
        "omitted",
        "next_offset",
        "resolution",
        "groups",
        "diagnostics",
        "hover",
        "provenance",
        "warnings",
    ]
    assert list(rendered)[:5] == expected_order[:5]
    assert list(rendered) == expected_order


def test_default_limit_is_ten_and_truncation_truth() -> None:
    rendered = render_navigation(_result(25), offset=0, limit=10)
    assert len(rendered["groups"][0]["locations"]) <= 10
    assert rendered["truncated"] is True
    assert rendered["next_offset"] == 10


def test_default_output_is_bounded_without_silent_clipping() -> None:
    rendered = render_navigation(_result(100), offset=0, limit=10)
    encoded = json.dumps(rendered, ensure_ascii=False, separators=(",", ":"))
    assert estimate_tokens(encoded) <= MAX_ESTIMATED_TOKENS
    assert rendered["truncated"] is True
    assert rendered["omitted"] >= 90


def test_stable_ordering_across_shuffled_input() -> None:
    import random

    shuffled = list(range(20))
    random.seed(42)
    random.shuffle(shuffled)
    locations_a = tuple(_location(start=i * 5, end=i * 5 + 3) for i in range(20))
    locations_b = tuple(locations_a[i] for i in shuffled)
    result_a = NavigationResult(
        NavigationStatus.OK,
        Capability.DEFINITIONS,
        Capability.DEFINITIONS,
        "pyright",
        "1.1.411",
        "repo",
        "checkout",
        "abc",
        "abc",
        1,
        PositionEncoding.UTF8,
        "query_ready",
        None,
        20,
        0,
        DEFAULT_LIMIT,
        locations_a,
        (),
        None,
        ResolutionLabel.LSP_CONFIRMED,
        _PROVENANCE,
        (),
    )
    result_b = NavigationResult(
        NavigationStatus.OK,
        Capability.DEFINITIONS,
        Capability.DEFINITIONS,
        "pyright",
        "1.1.411",
        "repo",
        "checkout",
        "abc",
        "abc",
        1,
        PositionEncoding.UTF8,
        "query_ready",
        None,
        20,
        0,
        DEFAULT_LIMIT,
        locations_b,
        (),
        None,
        ResolutionLabel.LSP_CONFIRMED,
        _PROVENANCE,
        (),
    )
    rendered_a = render_navigation(result_a, offset=0, limit=20)
    rendered_b = render_navigation(result_b, offset=0, limit=20)
    assert rendered_a["groups"] == rendered_b["groups"]


def test_byte_identical_output_for_repeated_input() -> None:
    result = _result(5)
    first = render_navigation(result, offset=0, limit=10)
    second = render_navigation(result, offset=0, limit=10)
    assert json.dumps(first, sort_keys=True) == json.dumps(
        second, sort_keys=True
    )


def test_offset_past_total_returns_empty_window() -> None:
    rendered = render_navigation(_result(5), offset=100, limit=10)
    assert rendered["groups"] == []
    assert rendered["next_offset"] is None
    assert rendered["truncated"] is False


def test_limit_boundaries() -> None:
    with pytest.raises(ValueError):
        render_navigation(_result(5), limit=0)
    with pytest.raises(ValueError):
        render_navigation(_result(5), limit=101)
    rendered = render_navigation(_result(5), limit=100)
    assert rendered["limit"] == 100


def test_repository_never_exposes_absolute_root() -> None:
    rendered = render_navigation(_result(1))
    assert set(rendered["repository"]) == {"repository_id", "checkout_id"}
    assert "checkout_root" not in rendered["repository"]


def test_hover_truncation_adds_warning() -> None:
    big_hover = "x" * 4096
    rendered = render_navigation(_result(1, hover=big_hover), offset=0, limit=10)
    assert "hover_truncated" in rendered["warnings"]


def test_signature_strips_source_body() -> None:
    location = _location(signature="def foo():\n    return 1\n    pass")
    result = NavigationResult(
        NavigationStatus.OK,
        Capability.DEFINITIONS,
        Capability.DEFINITIONS,
        "pyright",
        "1.1.411",
        "repo",
        "checkout",
        "abc",
        "abc",
        1,
        PositionEncoding.UTF8,
        "query_ready",
        None,
        1,
        0,
        DEFAULT_LIMIT,
        (location,),
        (),
        None,
        ResolutionLabel.LSP_CONFIRMED,
        _PROVENANCE,
        (),
    )
    rendered = render_navigation(result, offset=0, limit=10)
    sig = rendered["groups"][0]["locations"][0]["signature"]
    assert sig == "def foo():"
    assert "\n" not in sig


def test_include_source_disabled_by_default() -> None:
    rendered = render_navigation(_result(1), offset=0, limit=10)
    location_view = rendered["groups"][0]["locations"][0]
    assert "source" not in location_view


def test_grouping_by_path_and_symbol() -> None:
    locations = (
        _location("pkg/a.py", 0, 4, symbol="A"),
        _location("pkg/a.py", 10, 14, symbol="A"),
        _location("pkg/b.py", 0, 4, symbol="B"),
    )
    result = NavigationResult(
        NavigationStatus.OK,
        Capability.DEFINITIONS,
        Capability.DEFINITIONS,
        "pyright",
        "1.1.411",
        "repo",
        "checkout",
        "abc",
        "abc",
        1,
        PositionEncoding.UTF8,
        "query_ready",
        None,
        3,
        0,
        DEFAULT_LIMIT,
        locations,
        (),
        None,
        ResolutionLabel.LSP_CONFIRMED,
        _PROVENANCE,
        (),
    )
    rendered = render_navigation(result, offset=0, limit=10)
    assert len(rendered["groups"]) == 2
    assert rendered["groups"][0]["path"] == "pkg/a.py"
    assert len(rendered["groups"][0]["locations"]) == 2
