"""A fallback in an answer path keeps its answer and names the exception behind it.

The reranker answered `reranker_error`, the graph backend `graph_error`, the code
graph fell back to regex parsing, and a failed language-server request was
answered `Internal error`, each with the exception dropped: a broken model, a
broken graph and a missing grammar all looked alike. The fallback behaviour is
unchanged; the cause now reaches `vault_status.retrieval_degradations`, the parse
result, or the protocol's warning channel. The class guard is
`tests/test_a_broad_handler_carries_its_cause.py`.
See docs/research/2026-09-28-a-check-names-its-cause.md.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import code_graph  # noqa: E402
import lsp_protocol  # noqa: E402
import reranker  # noqa: E402
import retrieval  # noqa: E402
import search_memory  # noqa: E402


def _hit(candidate_id: str, path: str, score: float) -> dict:
    return {"candidate_id": candidate_id, "relative_path": path, "path": path, "score": score}


def _raise(error: BaseException):
    def call(*_args: object, **_kwargs: object):
        raise error

    return call


def test_a_failed_reranker_scorer_keeps_the_order_and_records_why(monkeypatch) -> None:
    monkeypatch.setattr(search_memory, "_DEGRADATIONS", {})
    docs = [{"path": f"{name}.md", "content": name, "score": 1.0} for name in "ab"]

    result = reranker.rerank("q", docs, limit=2, scorer=_raise(RuntimeError("scorer down")))

    assert [item["path"] for item in result] == ["a.md", "b.md"]
    assert result[0]["reranker_fallback_reason"] == "reranker_error"
    assert search_memory.degradation_reasons() == {"reranker_scoring": "RuntimeError: scorer down"}


def test_a_failed_graph_backend_falls_back_and_records_why(monkeypatch) -> None:
    monkeypatch.setattr(search_memory, "_DEGRADATIONS", {})

    result = retrieval.retrieve(
        "what calls target",
        requested_profile="GRAPH",
        lexical_backend=lambda **_filters: [_hit("seed", "seed.md", 1.0)],
        graph_backend=_raise(OSError("graph unavailable")),
        corpus_generation="gen-22",
        rerank_enabled=False,
    )

    assert result.trace.fallback_reason == "graph_error"
    assert search_memory.degradation_reasons() == {"graph_backend": "OSError: graph unavailable"}


def test_a_regex_fallback_names_the_grammar_that_would_not_load(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(code_graph, "_ts", {})
    monkeypatch.setattr(
        code_graph.importlib, "import_module", _raise(ImportError("tree_sitter_javascript"))
    )
    source = tmp_path / "a.js"
    source.write_text("function entry() { return helper(); }\n", encoding="utf-8")

    result = code_graph.parse_file(source)

    assert [item["name"] for item in result["functions"]] == ["entry"]
    assert result["parser_fallback_reason"] == "ImportError: tree_sitter_javascript"


def test_a_call_jedi_could_not_infer_says_why(tmp_path) -> None:
    script = SimpleNamespace(infer=_raise(ValueError("line out of range")))
    call = {"name": "helper", "line": 1, "confidence": "unknown", "semantic_eligible": True}

    enriched = code_graph._enriched_call(script, call, tmp_path)

    assert enriched == {**call, "semantic_error": "ValueError: line out of range"}


def test_a_failed_server_request_is_answered_internal_error_and_warned_with_its_cause() -> None:
    written: list[dict] = []
    warned: list[str] = []
    protocol = SimpleNamespace(
        _server_request_handlers={"workspace/configuration": _raise(KeyError("section"))},
        _write_message=written.append,
        _warn=warned.append,
    )
    message = {"id": 7, "method": "workspace/configuration", "params": {}}

    lsp_protocol.LspProtocol._handle_server_request(protocol, message)

    assert written == [
        {"jsonrpc": "2.0", "id": 7, "error": {"code": -32603, "message": "Internal error"}}
    ]
    assert warned == ["server request workspace/configuration failed: KeyError: 'section'"]
