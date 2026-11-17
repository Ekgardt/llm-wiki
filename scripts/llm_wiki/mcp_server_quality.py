"""The quality verdict and operation envelope of every answer."""
from __future__ import annotations

from llm_wiki import runtime_roots
from llm_wiki.mcp_server_freshness import (
    _degrade_stale_envelope,
)
from llm_wiki.mcp_server_limits import RETRIEVAL_TOOLS
from llm_wiki.mcp_server_sanitize import (
    _degrade_quality,
    _is_rendered_navigation_data,
    _sanitize_diagnostic,
    _sanitize_error_fields,
)
from mcp_contract import build_envelope


def quality_for(
    name: str,
    data,
    arguments: dict | None = None,
    *,
    limit_clamped: bool = False,
) -> dict:
    """Infer conservative operation quality only from returned evidence.

    The rules are tried in order and the first one that recognises the result
    answers; order is behaviour, because `get_architecture` is claimed by the
    impact rule before the code-graph rule ever sees it.
    """
    for rule in _QUALITY_RULES:
        quality = rule(name, data, arguments, limit_clamped)
        if quality is not None:
            return quality
    return {}


def _quality_of_error(name, data, arguments, limit_clamped) -> dict | None:
    if not isinstance(data, dict) or "error" not in data:
        return None
    return {
        "coverage": 0.0,
        "confidence": 0.2,
        "partial": True,
        "warnings": [str(data["error"])],
    }


def _is_grounded_recall(name: str, arguments: dict | None) -> bool:
    if name != "recall" or not arguments:
        return False
    return arguments.get("grounded") is True


def _citation_ids(citations) -> set:
    identifiers = set()
    for citation in citations:
        if isinstance(citation, dict) and citation.get("citation_id"):
            identifiers.add(citation.get("citation_id"))
    return identifiers


def _is_verified_claim(claim: object, citation_ids: set) -> bool:
    if not isinstance(claim, dict) or not claim.get("citation_ids"):
        return False
    return set(claim["citation_ids"]).issubset(citation_ids)


def _verified_claim_ratio(data: dict) -> float:
    """The share of claims whose citations all came back with the answer."""
    claims = data.get("claims", [])
    citation_ids = _citation_ids(data.get("citations", []))
    verified = sum(1 for claim in claims if _is_verified_claim(claim, citation_ids))
    return verified / len(claims) if claims else 0.0


# The ceiling the grounded path has always applied: a verified citation says the
# span exists and shares content with the claim, never that it entails it.
GROUNDED_CONFIDENCE_CEILING = 0.8


def _cited_paths(data: dict) -> list[str]:
    citations = [item for item in data.get("citations", []) if isinstance(item, dict)]
    return sorted({str(item.get("relative_path") or "") for item in citations})


def _cited_page_facts(data: dict) -> list:
    """What the vault knows about each page this answer cites."""
    from page_facts import read_page_facts

    facts = [read_page_facts(runtime_roots.vault_root(), path) for path in _cited_paths(data)]
    return [item for item in facts if item is not None]


def _weakest_stated_confidence(facts: list) -> float:
    """A chain of claims is worth its weakest evidence, not its average."""
    from page_facts import UNSTATED_CONFIDENCE

    if not facts:
        return UNSTATED_CONFIDENCE
    return min(item.stated_confidence() for item in facts)


def _page_warnings(item) -> list[str]:
    aging = (
        f"{item.relative_path} is {item.age_days} days old; a {item.page_type} "
        f"page stays current for {item.age_limit_days}."
    )
    candidates = (
        (item.confidence == "low", f"{item.relative_path} states confidence: low."),
        (
            item.authority == "inferred",
            f"{item.relative_path} is inferred, not stated by anyone.",
        ),
        (bool(item.aging), aging),
    )
    return [message for flagged, message in candidates if flagged]


def _provenance_warnings(facts: list) -> list[str]:
    warnings: list[str] = []
    for item in facts:
        warnings.extend(_page_warnings(item))
    return warnings


def _grounded_answer_quality(data: dict) -> dict:
    """Coverage and confidence from the answer and its sources, not from a constant.

    Coverage is the share of claims whose citations all came back verified.
    Confidence multiplies that by the weakest cited page's own stated
    confidence, so an answer resting on a `confidence: low` page cannot report
    more than that page does. Warnings name the page and the reason; a warning
    that appears on every answer is one nobody reads.
    """
    ratio = _verified_claim_ratio(data)
    facts = _cited_page_facts(data)
    confidence = min(
        GROUNDED_CONFIDENCE_CEILING, ratio * _weakest_stated_confidence(facts)
    )
    return {
        "coverage": round(ratio, 4),
        "confidence": round(confidence, 4),
        "partial": ratio < 1.0,
        "warnings": _provenance_warnings(facts),
    }


def _grounded_abstention_quality(data) -> dict:
    reason = data.get("reason") if isinstance(data, dict) else None
    return {
        "coverage": 0.0,
        "confidence": 0.0,
        "partial": True,
        "warnings": [str(reason or "Grounded QA abstained without a reason.")],
    }


def _quality_of_grounded_recall(name, data, arguments, limit_clamped) -> dict | None:
    if not _is_grounded_recall(name, arguments):
        return None
    if isinstance(data, dict) and data.get("status") == "answered":
        return _grounded_answer_quality(data)
    return _grounded_abstention_quality(data)


def _no_results_quality() -> dict:
    """A fresh dict every time: `_degrade_quality` writes into what it is given."""
    return {
        "coverage": 0.1,
        "confidence": 0.3,
        "fallback": True,
        "partial": True,
        "warnings": ["Search returned no results; retrieval coverage is unknown."],
    }


def _bm25_only_quality() -> dict:
    return {
        "coverage": 0.6,
        "confidence": 0.6,
        "fallback": True,
        "partial": True,
        "warnings": ["Only BM25 retrieval evidence is available."],
    }


def _carries_score(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    return any(key in result for key in ("fused_score", "vector_score"))


def _has_fused_scores(results) -> bool:
    return any(_carries_score(result) for result in results)


def _contradiction_candidate_quality(data: list) -> dict:
    if not data:
        return _degrade_quality(
            _no_results_quality(),
            "Contradiction candidates are unverified.",
            coverage=0.6,
            confidence=0.45,
        )
    quality = (
        {"coverage": 0.9, "confidence": 0.8}
        if _has_fused_scores(data)
        else _bm25_only_quality()
    )
    return _degrade_quality(
        quality,
        "Contradiction candidates are unverified.",
        coverage=0.6,
        confidence=0.45,
    )


def _is_verified_validity(validity: object) -> bool:
    if not isinstance(validity, dict):
        return False
    return validity.get("status") == "verified"


def _contradiction_claim_quality(data) -> dict:
    fields = data if isinstance(data, dict) else {}
    evidence = fields.get("evidence", [])
    if _is_verified_validity(fields.get("validity", {})):
        return {
            "coverage": 0.9 if evidence else 0.7,
            "confidence": 0.9,
            "partial": False,
            "warnings": [],
        }
    return {
        "coverage": 0.5 if evidence else 0.2,
        "confidence": 0.35,
        "fallback": True,
        "partial": True,
        "warnings": ["Claim evidence is unsupported; recommendations are quarantined."],
    }


def _quality_of_contradiction(name, data, arguments, limit_clamped) -> dict | None:
    if name != "check_contradiction":
        return None
    if isinstance(data, list):
        return _contradiction_candidate_quality(data)
    return _contradiction_claim_quality(data)


def _results_quality(results) -> dict:
    if not isinstance(results, list) or not results:
        return _no_results_quality()
    if not _has_fused_scores(results):
        return _bm25_only_quality()
    return {"coverage": 0.9, "confidence": 0.8}


# A trace that could not be recovered is not a retrieval fallback: nothing
# ran in place of anything else.
NOT_A_FALLBACK = frozenset({"trace_unavailable"})


def _fell_back(reason: object) -> bool:
    return bool(reason) and reason not in NOT_A_FALLBACK


def _with_trace_state(quality: dict, trace: object) -> dict:
    """The outer envelope says what the trace says (#26.1).

    A recall whose optional stage timed out carried `partial: true` and
    `fallback_reason: optional_stage_timeout` in every row and in the trace,
    while the envelope said `partial: false, fallback: false, warnings: []`.
    """
    if not isinstance(trace, dict):
        return quality
    merged = dict(quality)
    if trace.get("partial"):
        merged["partial"] = True
    return _with_fallback_state(merged, trace.get("fallback_reason"))


def _with_fallback_state(merged: dict, reason: object) -> dict:
    if not _fell_back(reason):
        return merged
    merged["fallback"] = True
    merged["warnings"] = [*merged.get("warnings", []), f"Retrieval fell back: {reason}."]
    return merged


def quality_of_results(name, data, arguments, limit_clamped) -> dict | None:
    if name not in RETRIEVAL_TOOLS:
        return None
    quality = _named_results_quality(name, data)
    if not limit_clamped:
        return quality
    return _degrade_quality(
        quality,
        "Requested limit was clamped to the safe 1-20 range.",
        coverage=0.8,
        confidence=0.8,
    )


def _named_results_quality(name: str, data) -> dict:
    """Both retrieval tools carry their rows under `results`, and a trace."""
    del name
    quality = _results_quality(data.get("results", []))
    return _with_trace_state(quality, data.get("retrieval_trace"))


def _impact_warnings(data, fallback: str) -> list:
    return list(data.get("warnings", [])) or [fallback]


def _impact_quality(data) -> dict:
    classification = data.get("classification") if isinstance(data, dict) else None
    if classification == "exact":
        return {"coverage": 0.9, "confidence": 0.9}
    if classification == "conservative":
        return {
            "coverage": 0.6,
            "confidence": 0.55,
            "partial": True,
            "warnings": _impact_warnings(data, "Impact analysis is conservative."),
        }
    return {
        "coverage": 0.2,
        "confidence": 0.25,
        "partial": True,
        "warnings": _impact_warnings(data, "Impact analysis is unresolved."),
    }


def _quality_of_impact(name, data, arguments, limit_clamped) -> dict | None:
    if name != "get_architecture" or not arguments:
        return None
    if arguments.get("mode") != "impact":
        return None
    return _impact_quality(data)


def _quality_of_navigation(name, data, arguments, limit_clamped) -> dict | None:
    if not _is_rendered_navigation_data(name, data):
        return None
    partial = data["status"] != "ok"
    return {
        "coverage": 0.5 if partial else 0.9,
        "confidence": 0.4 if partial else 0.85,
        "fallback": False,
        "partial": partial,
        "warnings": list(data.get("warnings", ())),
    }


def _active_graph_quality(data: dict) -> dict:
    unresolved = data.get("unresolved_count")
    if data.get("graph_complete") is True and unresolved == 0:
        return {
            "coverage": 0.95,
            "confidence": 0.9,
            "fallback": False,
            "partial": False,
            "warnings": [],
        }
    return {
        "coverage": 0.8,
        "confidence": 0.75,
        "fallback": False,
        "partial": True,
        "warnings": [f"Active code graph has {unresolved} unresolved observations."],
    }


def _repository_index_quality(data: dict) -> dict | None:
    """CODE-03 answers are exact: they did the thing, or refused by name.

    Without this they fell through the graph-completeness branch below and were
    labelled "Live static extraction fallback is incomplete", which is not just
    imprecise but false -- a listing of what is indexed extracts nothing.
    """
    if not isinstance(data, dict) or data.get("schema_version") != "repository-index/v1":
        return None
    if data.get("status") == "refused":
        return {
            "coverage": 0.0,
            "confidence": 1.0,
            "fallback": False,
            "partial": True,
            "warnings": [f"Refused: {data.get('reason')}"],
        }
    return {
        "coverage": 1.0,
        "confidence": 0.95,
        "fallback": False,
        "partial": bool(data.get("truncated") or data.get("paths_truncated")),
        "warnings": [],
    }


def quality_of_code_graph(name, data, arguments, limit_clamped) -> dict | None:
    if name not in {"find_dead_code", "get_architecture"}:
        return None
    index_quality = _repository_index_quality(data)
    if index_quality is not None:
        return index_quality
    return _graph_completeness_quality(data)


def _graph_completeness_quality(data) -> dict:
    if not isinstance(data, dict) or data.get("fallback") is not False:
        return {
            "coverage": 0.6,
            "confidence": 0.55,
            "fallback": True,
            "partial": True,
            "warnings": ["Live static extraction fallback is incomplete."],
        }
    return _active_graph_quality(data)


def _doctor_warning(prefix: str, data) -> str:
    codes = data.get("codes", []) if isinstance(data, dict) else []
    if not codes:
        return f"{prefix}."
    return f"{prefix}: {', '.join(str(code) for code in codes[:3])}"


def _unknown_doctor_quality(data) -> dict:
    from doctor import degraded_summary

    return {
        "coverage": 0.75,
        "confidence": 0.75,
        "partial": True,
        "warnings": [degraded_summary(data) or "Doctor health is unknown."],
    }


_DOCTOR_QUALITY = {
    "ok": lambda data: {"coverage": 0.9, "confidence": 0.85},
    "error": lambda data: {
        "coverage": 0.2,
        "confidence": 0.3,
        "partial": True,
        "warnings": [_doctor_warning("Doctor operator failed", data)],
    },
    "degraded": lambda data: {
        "coverage": 0.7,
        "confidence": 0.7,
        "partial": True,
        "warnings": [_doctor_warning("Doctor status is degraded", data)],
    },
}


def _quality_of_doctor(name, data, arguments, limit_clamped) -> dict | None:
    if name != "doctor":
        return None
    overall = data.get("overall_status") if isinstance(data, dict) else None
    build = _DOCTOR_QUALITY.get(overall)
    if build is None:
        return _unknown_doctor_quality(data)
    return build(data)


def _is_context_request(name: str, arguments: dict | None) -> bool:
    if name != "get_context":
        return False
    return bool(arguments)


def _missing_slugs(data) -> list:
    if not isinstance(data, dict):
        return []
    return data.get("missing_slugs", [])


def _context_quality(requested, missing) -> dict | None:
    if not requested or not missing:
        return None
    return {
        "coverage": max(0.0, (len(requested) - len(missing)) / len(requested)),
        "confidence": 0.8,
        "partial": True,
        "warnings": ["Some requested pages were unavailable."],
    }


def _quality_of_context(name, data, arguments, limit_clamped) -> dict | None:
    if not _is_context_request(name, arguments):
        return None
    return _context_quality(arguments.get("slugs", []), _missing_slugs(data))


# Order is behaviour: the first rule that recognises the result answers.
def _quality_of_vault_status(name, data, arguments, limit_clamped) -> dict | None:
    """The same compile health the health resource reports: never compiled is not full confidence."""
    if name != "vault_status" or not isinstance(data, dict):
        return None
    return _compile_health_quality(data)


# Tools whose successful answer is exact: a page read, a count of files, a write
# that either happened or raised. Every other tool must be claimed by a rule
# above, so an estimate never inherits the envelope's default full confidence
# (audit 2026-09-26 C-10: `vault_status` did, with no compile history at all).
EXACT_ANSWER_TOOLS = frozenset(
    {"read_page", "wiki_overview", "log_decision", "compile", "get_context"}
)


_QUALITY_RULES = (
    _quality_of_error,
    _quality_of_vault_status,
    _quality_of_grounded_recall,
    _quality_of_contradiction,
    quality_of_results,
    _quality_of_impact,
    _quality_of_navigation,
    quality_of_code_graph,
    _quality_of_doctor,
    _quality_of_context,
)


def _resource_quality(data) -> dict:
    if isinstance(data, dict) and "error" in data:
        return quality_for("resource", data)
    return _compile_health_quality(_resource_status(data))


def _resource_status(data) -> dict:
    status = data.get("status", data) if isinstance(data, dict) else {}
    if not isinstance(status, dict):
        return {}
    return status


def _backlog_quality(backlog) -> dict:
    if isinstance(backlog, (int, float)) and backlog > 0:
        return {
            "coverage": 0.7,
            "confidence": 0.7,
            "partial": True,
            "warnings": [f"Compile backlog contains {backlog} daily file(s)."],
        }
    return {"coverage": 0.9, "confidence": 0.85}


def _with_warmup_warning(quality: dict, status: dict) -> dict:
    """A failed warm-up is in the health answer, not only on a daemon thread."""
    warmup = status.get("warmup")
    if not isinstance(warmup, dict) or warmup.get("status") != "failed":
        return quality
    warnings = list(quality.get("warnings", []))
    warnings.append(
        f"Retrieval warm-up failed at {warmup.get('stage')}: {warmup.get('error')}; "
        "first answers may fall back to the lexical leg."
    )
    return {**quality, "partial": True, "warnings": warnings}


def _compile_health_quality(status: dict) -> dict:
    return _with_warmup_warning(_compile_quality(status), status)


def _compile_quality(status: dict) -> dict:
    compile_status = status.get("last_compile_status", "unknown")
    last_compile = status.get("last_compile", "never")
    if compile_status in {None, "unknown"} or last_compile in {None, "never"}:
        return {
            "coverage": 0.4,
            "confidence": 0.5,
            "partial": True,
            "warnings": ["Compile health is unknown."],
        }
    if compile_status not in {"ok", "success"}:
        return {
            "coverage": 0.6,
            "confidence": 0.6,
            "partial": True,
            "warnings": [f"Compile status is {compile_status}."],
        }
    return _backlog_quality(status.get("compile_backlog", 0))


def build_operation_envelope(
    data,
    quality: dict | None = None,
    *,
    components: dict[str, dict[str, object]] | None = None,
    index_timestamp: str | None = None,
    source_root: str | None = None,
) -> dict:
    envelope = build_envelope(
        data,
        components=components,
        index_timestamp=index_timestamp,
        source_root=source_root,
        **(quality or {}),
    )
    if components and envelope["freshness"] == "stale":
        _degrade_stale_envelope(envelope)
    envelope["warnings"] = _sanitize_diagnostic(envelope["warnings"])
    envelope["data"] = _sanitize_error_fields(envelope["data"])
    return envelope
