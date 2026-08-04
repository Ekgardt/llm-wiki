"""Deterministic navigation renderer contract tests."""

from __future__ import annotations

import json
from dataclasses import replace

import code_navigation_renderer as renderer
import pytest
from code_intelligence import (
    Capability,
    DiagnosticSeverity,
    PositionEncoding,
    PositionRange,
)
from code_navigation import (
    NavigationDiagnostic,
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
_TOP_LEVEL_KEYS = [
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


def _encoded(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


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


def _diagnostic(
    path: str = "pkg/api.py",
    start: int = 0,
    end: int = 4,
    *,
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR,
    code: str | None = None,
    message: str = "diagnostic",
    related: tuple[NavigationLocation, ...] = (),
    provenance: tuple[Provenance, ...] = _PROVENANCE,
) -> NavigationDiagnostic:
    return NavigationDiagnostic(
        path,
        PositionRange(start, end),
        severity,
        code,
        message,
        related,
        provenance,
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


def _diagnostic_result(
    diagnostics: tuple[NavigationDiagnostic, ...],
    *,
    status: NavigationStatus = NavigationStatus.OK,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    locations: tuple[NavigationLocation, ...] = (),
) -> NavigationResult:
    return replace(
        _result(0, status=status),
        requested_capability=Capability.DIAGNOSTICS,
        effective_capability=Capability.DIAGNOSTICS,
        total=len(diagnostics),
        offset=offset,
        limit=limit,
        locations=locations,
        diagnostics=diagnostics,
    )


def _rendered_location_count(rendered: dict[str, object]) -> int:
    return sum(len(group["locations"]) for group in rendered["groups"])  # type: ignore[index,union-attr]


def test_renderer_constants_are_exact() -> None:
    assert DEFAULT_LIMIT == 10
    assert MAX_LIMIT == 100
    assert MAX_ESTIMATED_TOKENS == 1_200
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abc") == 1


def test_renderer_puts_keys_in_exact_order() -> None:
    rendered = render_navigation(_result(1), offset=0, limit=10)
    assert list(rendered)[:5] == _TOP_LEVEL_KEYS[:5]
    assert list(rendered) == _TOP_LEVEL_KEYS


def test_default_limit_is_ten_and_truncation_truth() -> None:
    rendered = render_navigation(_result(25), offset=0, limit=10)
    assert len(rendered["groups"][0]["locations"]) <= 10
    assert rendered["truncated"] is True
    assert rendered["omitted"] == 15
    assert rendered["next_offset"] == 10
    assert rendered["status"] == NavigationStatus.PARTIAL.value


def test_default_output_is_bounded_without_silent_clipping() -> None:
    rendered = render_navigation(_result(100), offset=0, limit=10)
    encoded = _encoded(rendered)
    assert estimate_tokens(encoded) <= MAX_ESTIMATED_TOKENS
    assert rendered["truncated"] is True
    assert rendered["omitted"] == 90


def test_full_location_set_is_canonical_before_both_page_slices() -> None:
    alternate = Provenance("graph", "evidence-graph", "v2", "candidate")
    locations = (
        _location("pkg/z.py", 5, 8, symbol="Z", signature="def z():"),
        _location("pkg/a.py", 5, 8, symbol="A", signature="def b():"),
        replace(
            _location("pkg/a.py", 5, 8, symbol="A", signature="def a():"),
            provenance=(alternate,),
        ),
        _location("pkg/a.py", 0, 3, symbol=None),
        _location("pkg/a.py", 0, 3, symbol=""),
        _location("pkg/b.py", 1, 2, symbol="B"),
    )
    result_a = replace(_result(len(locations)), locations=locations)
    result_b = replace(
        result_a,
        locations=(locations[2], locations[5], locations[0], locations[4], locations[1], locations[3]),
    )

    for page_offset in (0, 3):
        rendered_a = render_navigation(result_a, offset=page_offset, limit=3)
        rendered_b = render_navigation(result_b, offset=page_offset, limit=3)
        assert _encoded(rendered_a) == _encoded(rendered_b)


def test_none_and_empty_containing_symbols_are_distinct_groups() -> None:
    locations = (
        _location("pkg/a.py", 0, 1, symbol=""),
        _location("pkg/a.py", 0, 1, symbol=None),
    )
    rendered = render_navigation(
        replace(_result(2), locations=locations),
        offset=0,
        limit=2,
    )

    assert [group["containing_symbol"] for group in rendered["groups"]] == [
        None,
        "",
    ]


def test_byte_identical_output_for_repeated_input() -> None:
    result = _result(5)
    first = render_navigation(result, offset=0, limit=10)
    second = render_navigation(result, offset=0, limit=10)
    assert _encoded(first) == _encoded(second)


def test_offset_past_total_returns_empty_window() -> None:
    rendered = render_navigation(_result(5), offset=100, limit=10)
    assert rendered["groups"] == []
    assert rendered["offset"] == 100
    assert rendered["limit"] == 10
    assert rendered["omitted"] == 0
    assert rendered["next_offset"] is None
    assert rendered["truncated"] is False
    assert rendered["status"] == NavigationStatus.OK.value


def test_complete_nonzero_offset_final_page_is_not_partial() -> None:
    rendered = render_navigation(_result(5), offset=3, limit=2)

    assert _rendered_location_count(rendered) == 2
    assert rendered["offset"] == 3
    assert rendered["omitted"] == 0
    assert rendered["next_offset"] is None
    assert rendered["truncated"] is False
    assert rendered["status"] == NavigationStatus.OK.value


def test_default_window_uses_result_values_and_explicit_values_override() -> None:
    result = replace(_result(8), offset=3, limit=2)

    defaulted = render_navigation(result)
    overridden = render_navigation(result, offset=1, limit=3)

    assert (defaulted["offset"], defaulted["limit"]) == (3, 2)
    assert defaulted["groups"][0]["locations"][0]["range"]["byte_start"] == 30
    assert (overridden["offset"], overridden["limit"]) == (1, 3)
    assert overridden["groups"][0]["locations"][0]["range"]["byte_start"] == 10


def test_limit_boundaries() -> None:
    with pytest.raises(ValueError):
        render_navigation(_result(5), limit=0)
    with pytest.raises(ValueError):
        render_navigation(_result(5), limit=101)
    rendered = render_navigation(_result(5), limit=100)
    assert rendered["limit"] == 100


@pytest.mark.parametrize(
    ("keyword", "value", "error"),
    [
        ("offset", True, TypeError),
        ("offset", 1.5, TypeError),
        ("offset", -1, ValueError),
        ("limit", True, TypeError),
        ("limit", 1.5, TypeError),
    ],
)
def test_window_values_reject_non_integer_boolean_and_invalid_ranges(
    keyword: str,
    value: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        render_navigation(_result(5), **{keyword: value})  # type: ignore[arg-type]


def test_offset_rejects_explicit_and_default_values_above_json_safe_integer() -> None:
    unsafe = 2**53

    with pytest.raises(ValueError, match="offset"):
        render_navigation(_result(0), offset=unsafe)
    with pytest.raises(ValueError, match="offset"):
        render_navigation(replace(_result(0), offset=unsafe))


def test_json_integer_metadata_must_stay_in_safe_range() -> None:
    unsafe = 2**53
    invalid_results = (
        replace(_result(0), document_version=unsafe),
        replace(
            _result(1),
            locations=(replace(_location(), line=unsafe),),
        ),
    )

    for result in invalid_results:
        with pytest.raises(ValueError, match="JSON-safe integer"):
            render_navigation(result)


def test_repository_never_exposes_absolute_root() -> None:
    rendered = render_navigation(_result(1))
    assert set(rendered["repository"]) == {"repository_id", "checkout_id"}
    assert "checkout_root" not in rendered["repository"]


def test_hover_truncation_adds_warning() -> None:
    big_hover = "😀" * 600
    rendered = render_navigation(_result(1, hover=big_hover), offset=0, limit=10)

    assert len(rendered["hover"].encode("utf-8")) <= 2_048
    assert "hover_truncated" in rendered["warnings"]
    assert f"hover_original_bytes:{len(big_hover.encode('utf-8'))}" in rendered["warnings"]
    assert rendered["status"] == NavigationStatus.PARTIAL.value


def test_signature_strips_source_body() -> None:
    original = "def foo():\n    return 1\n    pass"
    location = _location(signature=original)
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
    assert "signature_truncated" in rendered["warnings"]
    assert f"signature_original_bytes:{len(original.encode('utf-8'))}" in rendered["warnings"]
    assert rendered["status"] == NavigationStatus.PARTIAL.value


def test_multibyte_signature_stays_on_utf8_boundary_and_reports_original_size() -> None:
    original = "def пример(" + "😀" * 400 + "):\n    return 1"
    rendered = render_navigation(
        replace(_result(1), locations=(_location(signature=original),)),
        offset=0,
        limit=1,
    )

    signature = rendered["groups"][0]["locations"][0]["signature"]
    assert signature.startswith("def пример(")
    assert "\n" not in signature
    assert len(signature.encode("utf-8")) <= 1_024
    assert f"signature_original_bytes:{len(original.encode('utf-8'))}" in rendered["warnings"]
    assert "signature_truncated" in rendered["warnings"]


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


def test_diagnostics_are_canonical_primary_pageable_facts() -> None:
    related = (_location("pkg/related.py", 2, 3, symbol=None),)
    diagnostics = (
        _diagnostic(
            "pkg/b.py",
            0,
            1,
            severity=DiagnosticSeverity.WARNING,
            code="B",
            message="third",
        ),
        _diagnostic(
            "pkg/a.py",
            5,
            6,
            severity=DiagnosticSeverity.ERROR,
            message="second",
        ),
        _diagnostic(
            "pkg/a.py",
            0,
            1,
            severity=DiagnosticSeverity.HINT,
            code="A",
            message="first",
            related=related,
        ),
    )
    result_a = _diagnostic_result(diagnostics)
    result_b = _diagnostic_result((diagnostics[1], diagnostics[0], diagnostics[2]))

    for page_offset in (0, 2):
        rendered_a = render_navigation(result_a, offset=page_offset, limit=2)
        rendered_b = render_navigation(result_b, offset=page_offset, limit=2)
        assert _encoded(rendered_a) == _encoded(rendered_b)

    first_page = render_navigation(result_a, offset=0, limit=1)
    second_page = render_navigation(result_a, offset=1, limit=1)
    first = first_page["diagnostics"][0]
    assert first["message"] == "first"
    assert type(first["severity"]) is str
    assert first["severity"] == DiagnosticSeverity.HINT.value
    assert first["code"] == "A"
    assert first["range"] == {"byte_start": 0, "byte_end": 1}
    assert len(first["related"]) == 1
    assert second_page["diagnostics"][0]["message"] == "second"
    assert second_page["groups"] == []
    assert second_page["offset"] == 1
    assert second_page["omitted"] == 1
    assert second_page["next_offset"] == 2
    assert second_page["truncated"] is True
    assert second_page["status"] == NavigationStatus.PARTIAL.value


def test_oversized_diagnostic_consumes_one_fact_and_next_page_progresses() -> None:
    message = "diagnostic-message-" * 2_000
    hover = "😀" * 600
    result = replace(
        _diagnostic_result(
            (
                _diagnostic("pkg/a.py", message=message),
                _diagnostic("pkg/b.py", message="later diagnostic"),
            )
        ),
        symbol="PublicApi",
        hover=hover,
        warnings=("original_warning",),
    )

    first_page = render_navigation(result, offset=0, limit=2)
    first_encoded = _encoded(first_page)
    assert first_page["diagnostics"] == []
    assert message not in first_encoded
    assert "output_token_bound" in first_page["warnings"]
    assert "fact_omitted_token_bound" in first_page["warnings"]
    assert "original_warning" in first_page["warnings"]
    assert "hover_truncated" in first_page["warnings"]
    assert f"hover_original_bytes:{len(hover.encode('utf-8'))}" in first_page["warnings"]
    assert first_page["provider"] == {"name": "pyright", "version": "1.1.411"}
    assert first_page["repository"] == {
        "repository_id": "repo",
        "checkout_id": "checkout",
    }
    assert first_page["freshness"] == {
        "workspace_revision_before": "abc123",
        "workspace_revision_after": "abc123",
        "current": "abc123",
    }
    assert first_page["symbol"] == "PublicApi"
    assert first_page["readiness"] == "query_ready"
    assert first_page["provenance"] == [
        {
            "source": "lsp",
            "provider": "pyright",
            "version": "1.1.411",
            "observation": "provider_reported",
        }
    ]
    assert first_page["omitted"] == 2
    assert first_page["next_offset"] == 1
    assert first_page["next_offset"] != first_page["offset"]
    assert first_page["truncated"] is True
    assert first_page["status"] == NavigationStatus.PARTIAL.value
    assert estimate_tokens(first_encoded) <= MAX_ESTIMATED_TOKENS

    second_page = render_navigation(result, offset=first_page["next_offset"], limit=2)
    assert [item["message"] for item in second_page["diagnostics"]] == [
        "later diagnostic"
    ]
    assert second_page["omitted"] == 0
    assert second_page["next_offset"] is None
    assert second_page["truncated"] is True
    assert second_page["status"] == NavigationStatus.PARTIAL.value
    assert "output_token_bound" not in second_page["warnings"]
    assert "fact_omitted_token_bound" not in second_page["warnings"]
    assert "hover_truncated" in second_page["warnings"]
    assert estimate_tokens(_encoded(second_page)) <= MAX_ESTIMATED_TOKENS


def test_single_oversized_diagnostic_counts_consumed_fact_as_omitted() -> None:
    message = "oversized" * 4_000

    rendered = render_navigation(
        _diagnostic_result((_diagnostic(message=message),)),
        offset=0,
        limit=1,
    )

    assert rendered["diagnostics"] == []
    assert rendered["next_offset"] is None
    assert rendered["omitted"] == 1
    assert rendered["truncated"] is True
    assert rendered["status"] == NavigationStatus.PARTIAL.value
    assert "output_token_bound" in rendered["warnings"]
    assert "fact_omitted_token_bound" in rendered["warnings"]
    assert estimate_tokens(_encoded(rendered)) <= MAX_ESTIMATED_TOKENS


def test_oversized_metadata_is_rejected_before_tiny_fact_is_rendered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location_views = 0
    location_view = renderer._location_view

    def counted_location_view(
        location: NavigationLocation,
    ) -> tuple[dict[str, object], tuple[str, ...]]:
        nonlocal location_views
        location_views += 1
        return location_view(location)

    monkeypatch.setattr(renderer, "_location_view", counted_location_view)
    result = replace(_result(1), warnings=("warning" * 4_000,))

    with pytest.raises(ValueError, match="metadata exceeds token bound"):
        render_navigation(result, offset=0, limit=1)

    assert location_views == 0


def test_nonempty_token_reduction_always_adds_warning_and_partial_status() -> None:
    clipped_signature = "def kept():\n    return 1"
    locations = tuple(
        _location(
            start=index,
            end=index + 1,
            signature=(
                clipped_signature if index == 0 else "def " + "x" * 700
            ),
        )
        for index in range(10)
    )
    rendered = render_navigation(
        replace(_result(10), locations=locations),
        offset=0,
        limit=10,
    )
    rendered_count = _rendered_location_count(rendered)

    assert 0 < rendered_count < 10
    assert "output_token_bound" in rendered["warnings"]
    assert "signature_truncated" in rendered["warnings"]
    assert f"signature_original_bytes:{len(clipped_signature.encode('utf-8'))}" in rendered["warnings"]
    assert rendered["status"] == NavigationStatus.PARTIAL.value
    assert rendered["omitted"] == 10 - rendered_count
    assert rendered["next_offset"] == rendered_count
    assert estimate_tokens(_encoded(rendered)) <= MAX_ESTIMATED_TOKENS


def test_token_fitting_uses_bounded_monotonic_prefix_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostics = tuple(
        _diagnostic(
            start=index,
            end=index + 1,
            code=f"D{index:03d}",
            message="x" * 600,
        )
        for index in range(100)
    )
    diagnostic_views = 0
    payload_attempts = 0
    diagnostic_view = renderer._diagnostic_view
    assemble = renderer._assemble

    def counted_diagnostic_view(
        diagnostic: NavigationDiagnostic,
    ) -> tuple[dict[str, object], tuple[str, ...]]:
        nonlocal diagnostic_views
        diagnostic_views += 1
        return diagnostic_view(diagnostic)

    def counted_assemble(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal payload_attempts
        payload_attempts += 1
        return assemble(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(renderer, "_diagnostic_view", counted_diagnostic_view)
    monkeypatch.setattr(renderer, "_assemble", counted_assemble)

    rendered = render_navigation(
        _diagnostic_result(diagnostics),
        offset=0,
        limit=100,
    )

    assert 0 < len(rendered["diagnostics"]) < 100
    assert "output_token_bound" in rendered["warnings"]
    assert diagnostic_views <= 800
    assert payload_attempts <= 10
    assert estimate_tokens(_encoded(rendered)) <= MAX_ESTIMATED_TOKENS


def test_mixed_or_wrong_mode_fact_shapes_fail_closed() -> None:
    diagnostic = _diagnostic()
    location = _location()
    invalid_results = (
        replace(_result(1), diagnostics=(diagnostic,)),
        replace(_result(0), total=1, diagnostics=(diagnostic,)),
        replace(
            _diagnostic_result(()),
            total=1,
            locations=(location,),
        ),
        replace(
            _diagnostic_result((diagnostic,)),
            locations=(location,),
        ),
    )

    for result in invalid_results:
        with pytest.raises(ValueError, match="fact shape"):
            render_navigation(result)


@pytest.mark.parametrize(
    "result",
    [
        replace(_result(2), total=3),
        replace(_diagnostic_result((_diagnostic(),)), total=2),
    ],
)
def test_inconsistent_total_raises_before_paging(result: NavigationResult) -> None:
    with pytest.raises(ValueError, match="total"):
        render_navigation(result, offset=0, limit=2)


def test_pathological_metadata_raises_without_erasing_evidence() -> None:
    huge = "metadata" * 4_000
    result = replace(
        _result(0, hover=huge),
        provider=huge,
        provider_version=huge,
        repository_id=huge,
        checkout_id=huge,
        workspace_revision_before=huge,
        workspace_revision_after=huge,
        readiness=huge,
        symbol=huge,
        provenance=(Provenance(huge, huge, huge, huge),),
        warnings=(huge,),
    )

    with pytest.raises(ValueError, match="metadata exceeds token bound"):
        render_navigation(result)


def test_warnings_are_immutable_stably_deduplicated_and_sorted() -> None:
    original = ("z_warning", "a_warning", "z_warning")
    result = replace(_result(1), warnings=original)

    rendered = render_navigation(result)

    assert result.warnings == original
    assert rendered["warnings"] == ("a_warning", "z_warning")


def test_non_ok_status_is_preserved_when_window_omits_facts() -> None:
    rendered = render_navigation(
        _result(5, status=NavigationStatus.ERROR),
        offset=1,
        limit=1,
    )

    assert rendered["status"] == NavigationStatus.ERROR.value
