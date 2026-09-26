"""A leg the run asked for and did not get is reported missing (audit 2026-09-26 C-10).

docs/research/2026-09-26-a-missing-leg-is-reported.md
"""
from __future__ import annotations

import mcp_server
from retrieval import analyze_query, planned_request, retrieve

RELATION = "what depends on the capture queue"


def _hit(candidate_id: str) -> dict[str, object]:
    return {"candidate_id": candidate_id, "parent_id": f"{candidate_id}.md",
            "relative_path": f"{candidate_id}.md", "source_sha256": "a" * 64,
            "byte_start": 0, "byte_end": 10, "score": 1.0, "title": candidate_id,
            "content": "capture queue"}


def _graph_run_without_dense():
    requested, signals = planned_request(None, analyze_query(RELATION), semantic=True)
    return retrieve(
        RELATION,
        requested_profile=requested,
        signals=signals,
        lexical_backend=lambda **_filters: [_hit("seed")],
        dense_backend=None,
        graph_backend=lambda **_options: [],
        rerank_enabled=False,
    )


def test_a_planned_graph_run_says_what_it_asked_for() -> None:
    trace = _graph_run_without_dense().trace

    assert (trace.requested_mode, trace.signals_requested) == ("GRAPH", ("lexical", "dense", "graph"))


def test_a_graph_answer_without_its_dense_leg_reports_dense_missing() -> None:
    from dataclasses import asdict

    trace = mcp_server._reported_trace(asdict(_graph_run_without_dense().trace))

    components = mcp_server._recall_components({"retrieval_trace": trace})

    assert components["dense"]["freshness"] == "missing"


def test_no_trace_field_is_dropped_on_its_way_to_the_envelope() -> None:
    import json
    from dataclasses import asdict, fields

    from retrieval import RetrievalTrace

    names = {field.name for field in fields(RetrievalTrace)}
    schema = json.loads(mcp_server.RETRIEVAL_TRACE_SCHEMA.read_text(encoding="utf-8"))
    reported = mcp_server._reported_trace(asdict(_graph_run_without_dense().trace))

    assert (names - set(schema["properties"]), names - set(reported)) == (set(), set())
